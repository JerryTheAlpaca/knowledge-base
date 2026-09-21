"""OpenAI-compatible 文本 LLM 适配器（docs/02 §11.1；docs/20 §6.5）。

- endpoint + model + 参数只来自同用户已保存配置；不给模型工具权限。
- 两种调用：generate() 单轮（system+user，供现有单篇提炼）；
  generate_conversation() 真实多轮（按顺序的角色消息，供需求澄清与代码会话）。
  两者共用同一份 HTTP 发送与错误分类，不复制第二套客户端。
- 能力配置（capabilities）决定是否发送 temperature / json_object / 缓存字段。
  未核验的兼容网关默认 cache_mode=unknown：保留稳定 messages，但不注入任何
  未经支持的私有字段（docs/20 §6.5.4）。
- 错误分类对应 docs/02 §8.3：
  - 401/403            -> ProviderAuthFailed    -> waiting_key
  - 429 / 5xx / 连接失败 -> ProviderRetryable    -> retry_wait 退避
  - 已发出但超时未响应   -> ProviderOutcomeUnknown -> unknown_outcome（结果未知，不盲目重发）
  - 其余 4xx           -> ProviderInvalidRequest -> 失败（配置问题，重试无意义）
- 明文 Key 只在单次请求对象中使用，不落日志；SDK 请求对象不进异常堆栈。
- usage 只做技术计数归一化（缓存命中与成本诊断），不恢复平台扣费或金额账本。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

DEFAULT_TIMEOUT_SECONDS = 90
# 可回放的续接块：只保留协议要求的字段，不把整份响应塞进 metadata
REPLAY_MESSAGE_KEYS = ("reasoning_content", "provider_specific_fields", "tool_calls")
CACHE_MODES = ("unknown", "auto", "prompt_cache_key")
USAGE_PROTOCOLS = ("openai_chat", "openai_responses", "deepseek", "anthropic", "unknown")
RETENTION_VALUES = ("in_memory", "24h")


class ProviderError(Exception):
    """供应商调用失败基类。message 不含 Key 与完整请求体。"""

    retryable = False
    outcome_unknown = False


class ProviderAuthFailed(ProviderError):
    retryable = False


class ProviderRetryable(ProviderError):
    retryable = True


class ProviderOutcomeUnknown(ProviderError):
    """请求可能已送达并被计费，但未收到可确认的响应。"""

    retryable = False
    outcome_unknown = True


class ProviderInvalidRequest(ProviderError):
    """4xx：配置或请求本身有问题（模型名错误、参数不支持等）。"""

    retryable = False


@dataclass
class ConversationMessage:
    """一条按角色排列的消息。content 是实际发给模型（或从模型收到）的原文。

    metadata 保存供应商协议必须原样回传的续接块；它不是用户可见对话，
    也不能由客户端伪造（服务端装配时只认自己保存的值）。
    """

    role: str
    content: str
    metadata: dict = field(default_factory=dict)

    def as_request_message(self) -> dict:
        message: dict = {"role": self.role, "content": self.content}
        for key, value in self.metadata.items():
            if key in REPLAY_MESSAGE_KEYS and value is not None:
                message[key] = value
        return message


@dataclass
class GenerateRequest:
    system: str
    user: str
    max_output_tokens: int = 2000
    temperature: float | None = None
    json_mode: bool = False


@dataclass
class ConversationRequest:
    messages: list[ConversationMessage]
    max_output_tokens: int = 2000
    temperature: float | None = None
    json_mode: bool = False
    # 调用方（分享编排器）用应用专用 HMAC 派生后传入；本适配器只按能力白名单发送
    cache_policy: dict = field(default_factory=dict)


@dataclass
class GenerateResult:
    output_text: str
    provider_request_id: str | None = None
    finish_reason: str | None = None
    raw: dict = field(default_factory=dict)
    # 可直接追加进历史并回放的 assistant 消息（docs/20 §6.5.2 第 3 条）
    assistant_message: ConversationMessage | None = None
    usage: dict | None = None
    truncated: bool = False


def _int_or_none(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def normalize_usage(data: dict, protocol: str) -> dict | None:
    """把各协议的 token/缓存计数归一化，缺失一律 None（不能填 0）。

    归一化字段（docs/20 §6.5.5）：input_tokens_total 含命中部分，
    cache_read_tokens / cache_write_tokens / uncached_input_tokens 为其分解。
    """
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    out: dict = {
        "usage_protocol": protocol,
        "usage_available": False,
        "input_tokens_total": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "uncached_input_tokens": None,
    }
    if protocol == "deepseek":
        hit = _int_or_none(usage.get("prompt_cache_hit_tokens"))
        miss = _int_or_none(usage.get("prompt_cache_miss_tokens"))
        total = _int_or_none(usage.get("prompt_tokens"))
        if total is None and hit is not None and miss is not None:
            total = hit + miss
        out.update(cache_read_tokens=hit, uncached_input_tokens=miss)
    elif protocol == "openai_responses":
        total = _int_or_none(usage.get("input_tokens"))
        details = usage.get("input_tokens_details")
        out["cache_read_tokens"] = _int_or_none(details.get("cached_tokens")) if isinstance(details, dict) else None
    else:
        # openai_chat 与未核验网关：Chat Completions 的 prompt_tokens 已含命中输入
        total = _int_or_none(usage.get("prompt_tokens"))
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            out["cache_read_tokens"] = _int_or_none(details.get("cached_tokens"))
            out["cache_write_tokens"] = _int_or_none(details.get("cache_write_tokens"))
    if protocol == "anthropic":
        # 原生接口里 input_tokens 只是未缓存部分，总输入要三项相加
        base = _int_or_none(usage.get("input_tokens"))
        creation = _int_or_none(usage.get("cache_creation_input_tokens"))
        read = _int_or_none(usage.get("cache_read_input_tokens"))
        out["cache_read_tokens"] = read
        out["cache_write_tokens"] = creation
        parts = [x for x in (base, creation, read) if x is not None]
        total = sum(parts) if parts else None
    out["input_tokens_total"] = total
    out["output_tokens"] = _int_or_none(usage.get("completion_tokens") or usage.get("output_tokens"))
    if out["input_tokens_total"] is not None and out["uncached_input_tokens"] is None:
        if out["cache_read_tokens"] is not None:
            out["uncached_input_tokens"] = max(0, out["input_tokens_total"] - out["cache_read_tokens"])
    out["usage_available"] = any(
        out[k] is not None for k in ("input_tokens_total", "output_tokens", "cache_read_tokens")
    )
    return out


class OpenAICompatibleProvider:
    """同步适配器：Worker 进程内使用 httpx.Client。

    transport 参数仅供测试注入 httpx.MockTransport，生产不传。
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        capabilities: dict | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ):
        self.endpoint = self._chat_completions_url(endpoint)
        self.api_key = api_key
        self.model = model
        caps = capabilities or {}
        self.allow_temperature = bool(caps.get("temperature", True))
        self.json_mode = bool(caps.get("json_mode", True))
        # thinking_mode 显式设置（True/False）时向 DeepSeek 等 OpenAI 兼容服务
        # 发送 thinking 参数；未设置则不带该字段，沿用服务端默认行为。
        thinking = caps.get("thinking_mode")
        self.thinking_mode: bool | None = thinking if isinstance(thinking, bool) else None
        # 思考强度（reasoning_effort）：仅在开思考时随 thinking 一起发送
        # （api-docs.deepseek.com/guides/thinking_mode：low/high/max，默认 high）
        effort = caps.get("thinking_effort")
        self.thinking_effort: str | None = effort if isinstance(effort, str) else None
        self.timeout_seconds = int(caps.get("timeout_seconds") or timeout_seconds)
        # 多轮与缓存能力：来源于已核验的 endpoint＋model＋协议，不按模型名称猜测
        self.api_protocol = caps.get("api_protocol") if caps.get("api_protocol") in (
            "openai-chat", "openai-responses", "anthropic-messages") else "openai-chat"
        self.cache_mode = caps.get("cache_mode") if caps.get("cache_mode") in CACHE_MODES else "unknown"
        retention = caps.get("cache_retention")
        self.cache_retention = retention if retention in RETENTION_VALUES else None
        usage_protocol = caps.get("usage_protocol")
        self.usage_protocol = usage_protocol if usage_protocol in USAGE_PROTOCOLS else self._guess_usage_protocol()
        self._transport = transport

    def _guess_usage_protocol(self) -> str:
        host = httpx.URL(self.endpoint).host or ""
        if host.endswith("deepseek.com"):
            return "deepseek"
        if host == "api.openai.com":
            return "openai_chat"
        return "unknown"

    @staticmethod
    def _chat_completions_url(endpoint: str) -> str:
        base = endpoint.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    # ---- 请求装配 ----

    def _cache_fields(self, cache_policy: dict) -> dict:
        """只发送该 profile 已核验支持的缓存参数；未知网关一律不注入私有字段。"""
        if self.cache_mode != "prompt_cache_key":
            return {}
        key = cache_policy.get("prompt_cache_key")
        fields: dict = {}
        if isinstance(key, str) and 0 < len(key) <= 200:
            fields["prompt_cache_key"] = key
        retention = cache_policy.get("prompt_cache_retention")
        if self.cache_retention and retention in RETENTION_VALUES:
            fields["prompt_cache_retention"] = retention
        return fields

    def _body(self, messages: list[dict], *, max_output_tokens: int, temperature: float | None,
              json_mode: bool, cache_policy: dict | None = None) -> dict:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_output_tokens,
        }
        if temperature is not None and self.allow_temperature:
            body["temperature"] = temperature
        if json_mode and self.json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.thinking_mode is not None:
            # DeepSeek 思考模式开关（api-docs.deepseek.com/guides/thinking_mode）：
            # {"thinking": {"type": "enabled"|"disabled"}}，默认 enabled/effort=high；
            # 开思考且配置了档位时再带 reasoning_effort（low/high/max）
            body["thinking"] = {"type": "enabled" if self.thinking_mode else "disabled"}
            if self.thinking_mode and self.thinking_effort:
                body["reasoning_effort"] = self.thinking_effort
        body.update(self._cache_fields(cache_policy or {}))
        return body

    # ---- 调用 ----

    def generate(self, request: GenerateRequest) -> GenerateResult:
        """单轮调用：内部映射为两条消息，与多轮共用同一发送与分类逻辑。"""
        return self._call(self._body(
            [{"role": "system", "content": request.system}, {"role": "user", "content": request.user}],
            max_output_tokens=request.max_output_tokens, temperature=request.temperature,
            json_mode=request.json_mode,
        ))

    def generate_conversation(self, request: ConversationRequest) -> GenerateResult:
        if not request.messages:
            raise ProviderInvalidRequest("会话请求至少需要一条消息")
        messages = [m.as_request_message() for m in request.messages]
        return self._call(self._body(
            messages, max_output_tokens=request.max_output_tokens, temperature=request.temperature,
            json_mode=request.json_mode, cache_policy=request.cache_policy,
        ))

    def _call(self, body: dict) -> GenerateResult:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(self.timeout_seconds, connect=15.0)
        client_kwargs: dict = {"timeout": timeout}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport
        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(self.endpoint, json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # 连接未建立，请求未发出：可安全重试
            raise ProviderRetryable(f"连接供应商失败：{type(exc).__name__}") from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            # 请求可能已送达并计费：进入未知结果处理
            raise ProviderOutcomeUnknown(f"供应商响应超时：{type(exc).__name__}") from exc
        except httpx.HTTPError as exc:  # 其余网络错误保守可重试
            raise ProviderRetryable(f"网络错误：{type(exc).__name__}") from exc

        if resp.status_code in (401, 403):
            raise ProviderAuthFailed(f"模型凭据被拒绝（HTTP {resp.status_code}）")
        if resp.status_code == 429:
            raise ProviderRetryable("供应商限流（429）")
        if resp.status_code >= 500:
            raise ProviderRetryable(f"供应商临时错误（HTTP {resp.status_code}）")
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise ProviderInvalidRequest(f"模型请求被拒绝（HTTP {resp.status_code}）：{detail}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderRetryable("供应商返回非 JSON 响应") from exc
        choices = data.get("choices") or []
        if not choices:
            raise ProviderRetryable("供应商响应缺少 choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        finish_reason = choices[0].get("finish_reason")
        if not isinstance(content, str) or not content.strip():
            # 空正文多为思考型模型把 max_tokens 预算花在内部思考上（finish_reason=length），
            # 带上 finish_reason 便于在连接测试与任务错误里区分诊断
            hint = f"（finish_reason={finish_reason}）" if finish_reason else ""
            raise ProviderRetryable(f"供应商返回空内容{hint}")
        metadata = {k: message[k] for k in REPLAY_MESSAGE_KEYS if message.get(k) is not None}
        return GenerateResult(
            output_text=content,
            provider_request_id=resp.headers.get("x-request-id") or data.get("id"),
            finish_reason=finish_reason,
            raw=data,
            assistant_message=ConversationMessage(role="assistant", content=content, metadata=metadata),
            usage=normalize_usage(data, self.usage_protocol),
            truncated=finish_reason == "length",
        )


def parse_model_json(output_text: str) -> dict:
    """从模型输出解析 JSON 对象；容忍 ```json 代码块包裹。"""
    text = output_text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("输出中未找到 JSON 对象")
    doc = json.loads(text[start : end + 1])
    if not isinstance(doc, dict):
        raise ValueError("JSON 顶层必须是对象")
    return doc
