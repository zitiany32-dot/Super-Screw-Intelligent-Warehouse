from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from customsradar.ai.client import (
    AIClient,
    BudgetExceeded,
    BudgetTracker,
    InvalidModelOutput,
    ModelRefusal,
    Usage,
)


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


def _response(payload, stop_reason="end_turn", input_tokens=1000, output_tokens=500):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        content=[_Block(text)],
        stop_reason=stop_reason,
        stop_details=None,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("SDK 被调用的次数超出预期")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeSDK:
    """模拟 anthropic.Anthropic —— beta 路径故意 400，逼客户端降级到普通路径。"""

    def __init__(self, responses, beta_works=False):
        self.messages = FakeMessages(responses)
        beta_messages = FakeMessages(responses if beta_works else [])
        if not beta_works:
            beta_messages.create = self._reject_beta  # type: ignore[method-assign]
        self.beta = SimpleNamespace(messages=beta_messages)

    @staticmethod
    def _reject_beta(**kwargs):
        error = Exception("unknown beta")
        error.status_code = 400  # type: ignore[attr-defined]
        raise error


def _client(config, responses, beta_works=False, budget=None):
    sdk = FakeSDK(responses, beta_works=beta_works)
    return AIClient(config, budget=budget, client=sdk), sdk


def test_returns_parsed_json_and_prices_usage(config):
    client, sdk = _client(config, [_response({"ok": True})])
    data, usage = client.structured(system="s", user="u", schema={}, label="t")

    assert data == {"ok": True}
    # 1000 输入 @ $5/M + 500 输出 @ $25/M = 0.005 + 0.0125
    assert usage.cost_usd == pytest.approx(0.0175)
    assert client.budget.spent_usd == pytest.approx(0.0175)
    assert client.budget.calls == 1


def test_falls_back_to_non_beta_when_beta_rejected(config):
    client, sdk = _client(config, [_response({"ok": True})])
    client.structured(system="s", user="u", schema={}, label="t")
    assert sdk.messages.calls, "降级后应该走普通 messages.create"
    assert client._use_fallbacks is False

    # 第二次不再重试 beta 路径
    sdk.messages.responses.append(_response({"ok": 2}))
    client.structured(system="s", user="u", schema={}, label="t")
    assert len(sdk.messages.calls) == 2


def test_request_uses_structured_output_and_cached_system(config):
    client, sdk = _client(config, [_response({"ok": True})])
    schema = {"type": "object", "properties": {}}
    client.structured(system="sys", user="usr", schema=schema, label="t")

    kwargs = sdk.messages.calls[0]
    assert kwargs["model"] == config.model
    assert kwargs["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert kwargs["output_config"]["effort"] == config.effort
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_retries_on_invalid_json_then_succeeds(config):
    client, sdk = _client(
        config, [_response("这不是 JSON"), _response({"ok": True})]
    )
    data, _ = client.structured(system="s", user="u", schema={}, label="t")
    assert data == {"ok": True}
    assert len(sdk.messages.calls) == 2


def test_gives_up_after_max_attempts(config):
    client, _ = _client(config, [_response("nope")] * 3)
    with pytest.raises(InvalidModelOutput):
        client.structured(system="s", user="u", schema={}, label="t")


def test_refusal_raises(config):
    client, _ = _client(config, [_response({"ok": 1}, stop_reason="refusal")])
    with pytest.raises(ModelRefusal):
        client.structured(system="s", user="u", schema={}, label="t")


def test_budget_blocks_before_call(config):
    budget = BudgetTracker(limit_usd=0.01, spent_usd=0.02)
    client, sdk = _client(config, [_response({"ok": True})], budget=budget)
    with pytest.raises(BudgetExceeded):
        client.structured(system="s", user="u", schema={}, label="acme")
    assert sdk.messages.calls == [], "超预算时不该真的发请求"


def test_budget_accumulates_and_then_blocks(config):
    """预算是累加的：第一次调用没超，第二次超了就拦住 —— 对应「跑到一半停下」。"""
    budget = BudgetTracker(limit_usd=0.03)
    client, sdk = _client(
        config, [_response({"a": 1}), _response({"b": 2})], budget=budget
    )
    client.structured(system="s", user="u", schema={}, label="t1")
    assert budget.spent_usd == pytest.approx(0.0175)

    client.structured(system="s", user="u", schema={}, label="t2")
    assert budget.spent_usd == pytest.approx(0.035)
    assert budget.calls == 2

    with pytest.raises(BudgetExceeded):
        client.structured(system="s", user="u", schema={}, label="t3")
    assert len(sdk.messages.calls) == 2, "超预算之后不该再发请求"


def test_usage_addition():
    total = Usage(1, 2, cost_usd=0.5) + Usage(3, 4, cost_usd=0.25)
    assert (total.input_tokens, total.output_tokens) == (4, 6)
    assert total.cost_usd == pytest.approx(0.75)
