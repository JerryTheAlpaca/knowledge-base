"""OpenAICompatibleProvider 的 thinking 参数与请求体组装（DeepSeek 思考模式开关）。

thinking_mode 能力键：显式 True/False 时发送 {"thinking": {"type": ...}}，
未设置时不带该字段（沿用供应商默认行为，兼容非 DeepSeek 服务）。
thinking_effort 能力键：开思考时随请求发送 reasoning_effort（low/high/max），
关闭思考或未设置时不发送。
"""
from __future__ import annotations

import json

import httpx

from kbserver.providers.llm import GenerateRequest, OpenAICompatibleProvider


def _capture_transport(bodies: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        })

    return httpx.MockTransport(handler)


def _generate(caps: dict, bodies: list[dict]) -> None:
    provider = OpenAICompatibleProvider(
        endpoint="https://api.deepseek.com/v1",
        api_key="sk-test-1234567890",
        model="deepseek-chat",
        capabilities=caps,
        transport=_capture_transport(bodies),
    )
    provider.generate(GenerateRequest(system="s", user="u", max_output_tokens=64))


def test_thinking_mode_absent_sends_no_param():
    bodies: list[dict] = []
    _generate({}, bodies)
    assert "thinking" not in bodies[0]


def test_thinking_mode_enabled_and_disabled():
    bodies: list[dict] = []
    _generate({"thinking_mode": True}, bodies)
    assert bodies[0]["thinking"] == {"type": "enabled"}

    bodies2: list[dict] = []
    _generate({"thinking_mode": False}, bodies2)
    assert bodies2[0]["thinking"] == {"type": "disabled"}


def test_thinking_mode_alongside_other_params():
    bodies: list[dict] = []
    _generate({"thinking_mode": True, "json_mode": False, "temperature": False}, bodies)
    body = bodies[0]
    assert body["thinking"] == {"type": "enabled"}
    # 思考模式下仍按能力键决定其余字段；无响应格式要求时不发 response_format
    assert "response_format" not in body
    assert "temperature" not in body


def test_thinking_effort_sent_only_when_enabled():
    # 开思考 + 挡位：reasoning_effort 随请求发送（DeepSeek low/high/max）
    bodies: list[dict] = []
    _generate({"thinking_mode": True, "thinking_effort": "low"}, bodies)
    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["reasoning_effort"] == "low"

    # 关思考时不发挡位（thinking.type=disabled 即关闭，effort 无意义）
    bodies2: list[dict] = []
    _generate({"thinking_mode": False, "thinking_effort": "low"}, bodies2)
    assert bodies2[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in bodies2[0]

    # 开思考但未设置挡位：沿用服务端默认（high），不带字段
    bodies3: list[dict] = []
    _generate({"thinking_mode": True}, bodies3)
    assert bodies3[0]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in bodies3[0]
