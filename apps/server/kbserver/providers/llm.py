"""OpenAI-compatible 文本 LLM 适配器（docs/02 §11.1）。

- endpoint + model + 参数只来自同用户已保存配置；不给模型工具权限。
- 能力配置（capabilities）决定是否发送 temperature / json_object 等字段。
- 错误分类对应 docs/02 §8.3：
  - 401/403            -> ProviderAuthFailed    -> waiting_key
  - 429 / 5xx / 连接失败 -> ProviderRetryable    -> retry_wait 退避
  - 已发出但超时未响应   -> ProviderOutcomeUnknown -> unknown_outcome（结果未知，不盲目重发）
  - 其余 4xx           -> ProviderInvalidRequest -> 失败（配置问题，重试无意义）
- 明文 Key 只在单次请求对象中使用，不落日志；SDK 请求对象不进异常堆栈。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

DEFAULT_TIMEOUT_SECONDS = 90


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
class GenerateRequest:
    system: str
    user: str
    max_output_tokens: int = 2000
    temperature: float | None = None
    json_mode: bool = False


@dataclass
class GenerateResult:
    output_text: str
    provider_request_id: str | None = None
    finish_reason: str | None = None
    raw: dict = field(default_factory=dict)


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
        self.timeout_seconds = int(caps.get("timeout_seconds") or timeout_seconds)
        self._transport = transport

    @staticmethod
    def _chat_completions_url(endpoint: str) -> str:
        base = endpoint.strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def generate(self, request: GenerateRequest) -> GenerateResult:
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_tokens": request.max_output_tokens,
        }
        if request.temperature is not None and self.allow_temperature:
            body["temperature"] = request.temperature
        if request.json_mode and self.json_mode:
            body["response_format"] = {"type": "json_object"}

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
        if not isinstance(content, str) or not content.strip():
            # 空正文多为思考型模型把 max_tokens 预算花在内部思考上（finish_reason=length），
            # 带上 finish_reason 便于在连接测试与任务错误里区分诊断
            finish = choices[0].get("finish_reason")
            hint = f"（finish_reason={finish}）" if finish else ""
            raise ProviderRetryable(f"供应商返回空内容{hint}")
        return GenerateResult(
            output_text=content,
            provider_request_id=resp.headers.get("x-request-id") or data.get("id"),
            finish_reason=choices[0].get("finish_reason"),
            raw=data,
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
