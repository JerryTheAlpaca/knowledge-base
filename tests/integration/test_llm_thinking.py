"""OpenAICompatibleProvider 的 thinking 参数与请求体组装（DeepSeek 思考模式开关）。

thinking_mode 能力键：显式 True/False 时发送 {"thinking": {"type": ...}}，
未设置时不带该字段（沿用供应商默认行为，兼容非 DeepSeek 服务）。
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
