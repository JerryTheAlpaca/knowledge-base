"""真实多轮 messages 与缓存 usage（docs/20 §6.5；验收 A38/A40/A41/A43）。

这里只验请求装配与计数归一化：真实供应商的缓存命中属于实施/验收阶段用授权
配置完成的验证，不能用模拟响应代替（§6.5.6）。
"""
from __future__ import annotations

import json

import httpx

from kbserver.providers.llm import (
    ConversationMessage,
    ConversationRequest,
    GenerateRequest,
    OpenAICompatibleProvider,
    normalize_usage,
)


def _transport(bodies: list[dict], replies: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        reply = replies[len(bodies) - 1]
        return httpx.Response(200, json={
            "id": f"chatcmpl-{len(bodies)}",
            "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                      "prompt_tokens_details": {"cached_tokens": 80}},
        })

    return httpx.MockTransport(handler)


def _provider(bodies, replies, caps=None, host="api.deepseek.com"):
    return OpenAICompatibleProvider(
        endpoint=f"https://{host}/v1",
        api_key="sk-test-1234567890",
        model="deepseek-chat",
        capabilities=caps or {"cache_mode": "auto", "usage_protocol": "deepseek"},
        transport=_transport(bodies, replies),
    )


def test_three_rounds_only_append_and_prefix_identical():
    """A38：role 顺序与历史 assistant 原文保留，第二轮以后只在尾部新增。"""
    bodies: list[dict] = []
    replies = [
        json.dumps({"understanding": "先读材料", "questions": [{"id": "q1", "text": "读者是谁？"}],
                    "next_action": "ask_user"}, ensure_ascii=False),
        json.dumps({"understanding": "给初学者", "questions": [],
                    "next_action": "confirm_brief"}, ensure_ascii=False),
        json.dumps({"brief": {"goal": "比较差异"}, "questions": [],
                    "next_action": "confirm_brief"}, ensure_ascii=False),
    ]
    messages = [
        ConversationMessage(role="system", content="固定的创作与澄清规则"),
        ConversationMessage(role="user", content='{"sources": [材料包固定字节]}'),
        ConversationMessage(role="user", content="请把这些材料整合成便于分享的页面。"),
    ]
    for i in range(3):
        result = _provider(bodies, replies).generate_conversation(
            ConversationRequest(messages=list(messages), max_output_tokens=1200, json_mode=True)
        )
        # 回传模型的是实际收到的 assistant 消息，不是重新序列化的界面文本
        messages.append(result.assistant_message)
        if i < 2:
            messages.append(ConversationMessage(role="user", content=f"第 {i + 1} 轮回答"))

    first = bodies[0]["messages"]
    second = bodies[1]["messages"]
    third = bodies[2]["messages"]
    assert [m["role"] for m in first] == ["system", "user", "user"]
    assert [m["role"] for m in second] == ["system", "user", "user", "assistant", "user"]
    assert [m["role"] for m in third] == [
        "system", "user", "user", "assistant", "user", "assistant", "user"]
    # 前缀逐字节相同：早期消息没有被重排、没有被塞进轮次号或当前时间
    assert second[: len(first)] == first
    assert third[: len(second)] == second
    assert json.loads(third[3]["content"])["questions"][0]["text"] == "读者是谁？"
    assert bodies[0]["response_format"] == {"type": "json_object"}


def test_prefix_hash_detects_drift_and_ignores_business_state():
    """A40：前缀哈希只覆盖固定前缀；追加新消息不变，改 system 或早期材料才变。"""
    from kbserver.domain.share_conversations import prefix_hash, stable_prefix

    prefix = stable_prefix(system_prompt="固定的创作规则", pack_text='{"sources":[]}',
                           initial_request="做成分享页面")
    same = stable_prefix(system_prompt="固定的创作规则", pack_text='{"sources":[]}',
                         initial_request="做成分享页面")
    assert prefix_hash(prefix) == prefix_hash(same)
    # 业务状态（轮次、时间、最新摘要）作为尾部新消息追加，前缀部分不变
    from kbserver.domain.share_conversations import request_messages

    history = [
        ConversationMessage(role="assistant", content='{"next_action":"ask_user"}'),
        ConversationMessage(role="user", content="第 2 轮回答"),
    ]
    assembled = request_messages(prefix=prefix, history=history,
                                 tail=[ConversationMessage(role="user", content="第 3 轮回答")])
    assert prefix_hash(assembled[: len(prefix)]) == prefix_hash(prefix)
    drifted = stable_prefix(system_prompt="固定的创作规则（今天 21:00）", pack_text='{"sources":[]}',
                            initial_request="做成分享页面")
    assert prefix_hash(prefix) != prefix_hash(drifted)
    repacked = stable_prefix(system_prompt="固定的创作规则", pack_text='{"sources":[新片段]}',
                             initial_request="做成分享页面")
    assert prefix_hash(prefix) != prefix_hash(repacked)
    reordered = [prefix[1], prefix[0], prefix[2]]
    assert prefix_hash(prefix) != prefix_hash(reordered)


def test_deepseek_usage_hit_and_miss_not_double_counted():
    usage = normalize_usage({
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                  "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200},
    }, "deepseek")
    assert usage["input_tokens_total"] == 1000
    assert usage["cache_read_tokens"] == 800
    assert usage["uncached_input_tokens"] == 200
    assert usage["output_tokens"] == 50
    assert usage["usage_available"] is True


def test_openai_chat_usage_cached_tokens_are_part_of_total():
    usage = normalize_usage({
        "usage": {"prompt_tokens": 500, "completion_tokens": 30,
                  "prompt_tokens_details": {"cached_tokens": 384}},
    }, "openai_chat")
    assert usage["input_tokens_total"] == 500
    assert usage["cache_read_tokens"] == 384
    assert usage["uncached_input_tokens"] == 116
    assert usage["cache_write_tokens"] is None  # 没有该字段时不推测写入量


def test_missing_usage_fields_stay_null_not_zero():
    """A41：未知值不等于零，缺失不能填 0；没有 usage 时整体为空。"""
    provider = OpenAICompatibleProvider(
        endpoint="https://gateway.example/v1", api_key="sk-x", model="m", capabilities={},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})),
    )
    assert provider.generate(GenerateRequest(system="s", user="u")).usage is None

    partial = OpenAICompatibleProvider(
        endpoint="https://gateway.example/v1", api_key="sk-x", model="m", capabilities={},
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 12}})),
    )
    usage = partial.generate(GenerateRequest(system="s", user="u")).usage
    assert usage["output_tokens"] == 12
    assert usage["input_tokens_total"] is None
    assert usage["cache_read_tokens"] is None
    assert usage["uncached_input_tokens"] is None
    assert usage["usage_protocol"] == "unknown"
    assert usage["usage_available"] is True


def test_anthropic_native_usage_sums_three_parts():
    usage = normalize_usage({
        "usage": {"input_tokens": 20, "cache_creation_input_tokens": 300,
                  "cache_read_input_tokens": 700, "output_tokens": 40},
    }, "anthropic")
    assert usage["input_tokens_total"] == 1020
    assert usage["cache_read_tokens"] == 700
    assert usage["cache_write_tokens"] == 300
    assert usage["output_tokens"] == 40


def test_unverified_gateway_sends_no_private_cache_fields():
    """A43：cache_mode=unknown 时即使调用方给了键也不发送。"""
    bodies: list[dict] = []
    provider = _provider(bodies, ['{"ok":1}'], caps={"cache_mode": "unknown"})
    provider.generate_conversation(ConversationRequest(
        messages=[ConversationMessage(role="user", content="hi")],
        cache_policy={"prompt_cache_key": "abc123", "prompt_cache_retention": "24h"},
    ))
    assert "prompt_cache_key" not in bodies[0]
    assert "prompt_cache_retention" not in bodies[0]


def test_cache_key_only_for_validated_profile():
    bodies: list[dict] = []
    provider = _provider(bodies, ['{"ok":1}'], caps={
        "cache_mode": "prompt_cache_key", "cache_retention": "24h", "usage_protocol": "openai_chat"},
        host="api.openai.com")
    provider.generate_conversation(ConversationRequest(
        messages=[ConversationMessage(role="user", content="hi")],
        cache_policy={"prompt_cache_key": "kb-share-" + "a" * 40, "prompt_cache_retention": "24h"},
    ))
    assert bodies[0]["prompt_cache_key"].startswith("kb-share-")
    assert bodies[0]["prompt_cache_retention"] == "24h"


def test_length_truncated_output_is_flagged_not_repaired_into_success():
    bodies: list[dict] = []
    provider = _provider(bodies, ["半截输出"])
    provider._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "choices": [{"message": {"content": "半截"}, "finish_reason": "length"}]}))
    result = provider.generate_conversation(ConversationRequest(
        messages=[ConversationMessage(role="user", content="hi")]))
    assert result.truncated is True
    assert result.finish_reason == "length"
