"""dsh 子进程的适配层：整个编排服务里**唯一**直接调用 DeepSeek Harness SDK 的地方。

这么切不是为了好看：上游现在是 `0.2.0-rc.2` / SDK `0.1.5rc1`，README 明写「开发者
预览，未来会出现破坏兼容性的变更」。把交互面收敛进这一个类，升级时只改这一个文件，
也让「哪些是 dsh 的行为、哪些是我们的行为」在排障时分得清。

进程模型：**每用户一个长驻子进程**，一轮跑完不立刻退出；空闲到
`AGENT_IDLE_REAP_SECONDS` 且没有在途轮次时才回收。会话状态在 `$DSH_HOME` 的 JSONL
里，所以回收之后可以用 `session/resume` 接着聊。

凭据形态：LLM 会话 token 走子进程环境（`api_key=` → `DEEPSEEK_API_KEY`），MCP token
走 `KB_MCP_TOKEN`。dsh 那边的 header 是**启动时读一次的静态配置、没有刷新钩子**，
所以进程回收（默认 600s）刻意早于会话 token TTL（默认 900s）：重启即换新，
不需要在运行中改环境变量。真实模型 Key 从来不到这里。

降权走 SDK 公开的 `dsh_bin` 参数指向的包装脚本（`deploy/agent/dsh-drop`），
不用 monkeypatch SDK 内部的 Popen。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from .config import OrchestratorSettings

# 工具结果可能整篇原文进事件表：给一个硬上限，历史体积才不会失控
MAX_EVENT_TEXT_CHARS = 60_000
SUMMARY_CHARS = 800

# AGENT_DEBUG_EVENTS=1 时把翻译前的原始事件打出来：上游是 rc 线，字段名一改
# 界面就会静默少一类信息，没有这行日志就只能靠猜
import os as _os

_DEBUG = _os.environ.get("AGENT_DEBUG_EVENTS") == "1"


class _ResumeResponse(BaseModel):
    """session/resume 的响应只用来判断「能不能续」，不解释内容。"""

    model_config = ConfigDict(extra="allow")


class DshTurnError(Exception):
    """一轮没跑成。message 只放人话与异常类名，不含 Key、不含上游正文。"""


@dataclass(slots=True)
class _Sink:
    on_event: Callable[[str, dict], None]

    def __call__(self, notification: Any) -> None:
        if notification.method == "agent/status":
            self.on_event("status", {"text": _clip(_text_of(notification.payload, "status"),
                                                   SUMMARY_CHARS)})
            return
        if notification.method != "session.event":
            return
        event = notification.payload.get("event")
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "")
        translate = _EVENT_TRANSLATIONS.get(kind)
        if translate is None:
            # 其余是内部记账类事件（step/start、request/context…）：
            # 界面只说真实的阶段，不伪装思考流（docs/20 §3.5）
            return
        if _DEBUG:
            print(f"[dsh-event] {kind} "
                  f"{json.dumps(event.get('data'), ensure_ascii=False)[:600]}")
        payload = translate(event.get("data"))
        if payload is not None:
            self.on_event(_KIND_BY_TYPE[kind], payload)


class DshSessionProcess:
    """拥有一个用户的 dsh 子进程，把它的通知翻译成可入库的用户可见事件。"""

    def __init__(self, settings: OrchestratorSettings, *, home: Path, workspace: Path,
                 uid: int, mcp_token: str, llm_token: str, model: str, llm_base_url: str,
                 dsh_session_id: str, resumable: bool = False) -> None:
        self.settings = settings
        self.home = home
        self.workspace = workspace
        self.uid = uid
        self.dsh_session_id = dsh_session_id
        self.model = model
        self.llm_base_url = llm_base_url
        self._lock = threading.Lock()
        self._harness: Any = None
        self._llm_token = llm_token
        self._mcp_token = mcp_token
        # 全新会话没有可续的历史，去问 session/resume 只会白费一次往返并打一行误导日志
        self._resumed = not resumable

    # ---- 生命周期 ----

    def _build(self) -> Any:
        from deepseek_harness import DeepSeekHarness

        kwargs: dict[str, Any] = {
            "dsh_home": str(self.home),
            "cwd": str(self.workspace),
            "profile": "sdk",
            "provider": "deepseek-official",
            "model": self.model,
            "base_url": self.llm_base_url,
            "api_key": self._llm_token,
            "env": {"KB_MCP_TOKEN": self._mcp_token, "DSH_PERMISSION_MODE": "read-only"},
            "initialize_timeout_seconds": 60,
            "request_timeout_seconds": self.settings.turn_timeout_seconds,
        }
        if self.settings.drop_privileges:
            kwargs["dsh_bin"] = str(Path(__file__).resolve().parent.parent / "dsh-drop")
            kwargs["env"]["AGENT_DROP_UID"] = str(self.uid)
        harness = DeepSeekHarness(**kwargs)
        harness.start()
        return harness

    def ensure_started(self) -> None:
        with self._lock:
            if self._harness is None:
                self._harness = self._build()

    def stop(self) -> None:
        with self._lock:
            harness, self._harness = self._harness, None
            self._resumed = False
        if harness is None:
            return
        try:
            harness.close()
        except Exception:  # noqa: BLE001 —— 回收路径上的关闭失败不该冒成用户可见错误
            pass

    @property
    def alive(self) -> bool:
        return self._harness is not None

    def holds_tokens(self, token: dict) -> bool:
        """进程手里那两个凭据是否还是这一份。

        能力凭据会随 TTL 换新，拿着旧凭据撑着的进程下一轮调工具就会失败 ——
        所以「换 token 等于换进程」，而不是在运行中改环境变量（dsh 读不到）。
        """
        return (self._llm_token == token.get("token")
                and self._mcp_token == (token.get("mcp_token") or ""))

    # ---- 跑一轮 ----

    def run_turn(self, text: str, on_event: Callable[[str, dict], None]) -> None:
        """阻塞执行一轮。`on_event(kind, payload)` 由调用方落表，SSE 再从表追。

        失败**不自动重试在途轮次**：模型可能已经把这一轮算进上下文与费用
        （与 A 机 `ProviderOutcomeUnknown` 不盲目重发同一原则）。
        """
        self.ensure_started()
        with self._lock:
            harness = self._harness
        if harness is None:
            raise DshTurnError("运行时没起来")
        if not self._resumed:
            self._resume(harness)
        sink = _Sink(on_event)
        try:
            result = harness.run(text, session_id=self.dsh_session_id, on_notification=sink)
        except TimeoutError as exc:
            self._poison()
            raise DshTurnError("这一轮超时了；服务器没有替你重试，请确认后再发") from exc
        except Exception as exc:
            self._poison()
            kind = type(exc).__name__
            if _is_transport_loss(kind):
                raise DshTurnError("运行时中断了，这一轮的结果不确定；下一轮会自动重开") from exc
            raise DshTurnError(f"运行时报错（{kind}）") from exc
        if not result.events:
            self._poison()
            raise DshTurnError("这一轮没有产生任何可展示的事件")

    def _resume(self, harness: Any) -> None:
        """续上这个用户在 `$DSH_HOME` 里的历史，只在进程新建后的第一轮试一次。

        续不上（上游改了签名、旧历史格式不认识）就按新会话继续：A 机镜像里的历史
        照样读得到，界面不会假装「接着上次说完」。
        """
        self._resumed = True
        try:
            harness.client.request(
                "session/resume",
                {"sessionId": self.dsh_session_id, "cwd": str(self.workspace)},
                response_model=_ResumeResponse,
            )
        except Exception as exc:  # noqa: BLE001 —— 续接失败是可以继续的降级，不是崩溃
            print(f"[agent] session/resume 不可用，按新会话继续：{type(exc).__name__}")

    def _poison(self) -> None:
        """一轮失败就丢弃这个进程：状态不明的进程不该接着服务下一轮。"""
        thread = threading.Thread(target=self.stop, daemon=True)
        thread.start()

    def rotate_tokens(self, *, llm_token: str, mcp_token: str) -> None:
        """换 token 等于换进程（环境变量是启动时读一次的静态值）。"""
        self._llm_token = llm_token
        self._mcp_token = mcp_token
        self._poison()


def _is_transport_loss(kind: str) -> bool:
    return any(word in kind for word in ("Closed", "Transport", "EOF", "Pipe"))


def _text_of(data: dict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _clip(value: str, limit: int = MAX_EVENT_TEXT_CHARS) -> str:
    return value if len(value) <= limit else value[:limit]


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _message_text(raw: Any) -> dict | None:
    """assistant/user 消息：把 content 块里的文本拼起来；空文本不产生事件。"""
    data = raw if isinstance(raw, dict) else {}
    message = data.get("message") if isinstance(data.get("message"), dict) else data
    content = message.get("content")
    if isinstance(content, str):
        return {"text": _clip(content)} if content.strip() else None
    if not isinstance(content, list):
        return None
    parts = [str(b.get("text") or "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    text = "".join(parts)
    return {"text": _clip(text)} if text.strip() else None


# 上游是 rc 线，工具事件的字段名不属于稳定契约：先按已知几种叫法取，
# 全取不到就把整份 data 兜住，免得一次工具调用在界面上变成一个空条目
_NAME_KEYS = ("name", "toolName", "tool_name")
_CALL_KEYS = ("arguments", "args", "input", "parameters")
_RESULT_KEYS = ("content", "result", "output", "text", "toolResult")


def _pick(data: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if data.get(key) not in (None, "", [], {}):
            return data.get(key)
    return None


def _tool_call(raw: Any) -> dict | None:
    data = raw if isinstance(raw, dict) else {}
    name = _pick(data, _NAME_KEYS)
    arguments = _pick(data, _CALL_KEYS)
    if name is None and arguments is None:
        return {"name": "", "arguments": _clip(_stringify(data), SUMMARY_CHARS)}
    return {"name": _stringify(name), "arguments": _clip(_stringify(arguments), SUMMARY_CHARS)}


def _result_texts(value: Any) -> list[str]:
    """工具返回里的文本：`content[] → {type:text} → text`，一层层剥开而不是猜字段。"""
    out: list[str] = []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return [value["text"]]
        for key in ("content", "result", "output", "text"):
            if value.get(key) is not None:
                out.extend(_result_texts(value.get(key)))
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(_result_texts(item))
    return out


def _tool_result(raw: Any) -> dict | None:
    data = raw if isinstance(raw, dict) else {}
    name = _pick(data, _NAME_KEYS)
    texts = _result_texts(data.get("message", data))
    error = data.get("error") if isinstance(data.get("error"), dict) else None
    if error is not None:
        # 工具报错要如实出现在过程里：只写「工具返回」而不带失败，等于把问题咽下去
        label = _stringify(_pick(error, ("name", "code")) or "工具调用失败")
        summary = f"失败：{label}"
    elif texts:
        summary = "\n".join(texts)
    else:
        summary = _stringify(_pick(data, _RESULT_KEYS) or data)
    if name is None and not summary:
        return None
    return {"name": _stringify(name), "summary": _clip(summary, SUMMARY_CHARS)}


def _turn_end(raw: Any) -> dict | None:
    data = raw if isinstance(raw, dict) else {}
    reason = data.get("reason") if isinstance(data.get("reason"), dict) else {}
    return {"reason": _text_of(reason, "kind") or "unknown"}


# dsh 的事件类型 -> 用户可见事件名与翻译函数。表外的一律不进界面。
# 不翻译 `user/message`：提交那一轮时编排服务已经写过一条权威的 user_message。
# dsh 的 user 角色里还夹着 <system-reminder> 工作区指令与运行时上下文快照，
# 照搬会把它们当「用户说的话」画进界面，还会把它们镜像进 A 机库白占体积。
_TRANSLATIONS: dict[str, tuple[str, Callable[[Any], dict | None]]] = {
    "assistant/message": ("assistant_message", _message_text),
    "tool/call": ("tool_call", _tool_call),
    "tool/result": ("tool_result", _tool_result),
    "turn/end": ("turn_end", _turn_end),
}
_EVENT_TRANSLATIONS = {k: v[1] for k, v in _TRANSLATIONS.items()}
_KIND_BY_TYPE = {k: v[0] for k, v in _TRANSLATIONS.items()}
