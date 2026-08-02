"""Anthropic API 封装：结构化输出 + 重试 + 花费统计 + 预算硬上限。"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Config

logger = logging.getLogger(__name__)

# 结构化输出用的 beta 头（refusal 自动降级到备用模型）
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class BudgetExceeded(RuntimeError):
    """当晚 AI 预算已用完，剩下的公司留到下一批。"""


class ModelRefusal(RuntimeError):
    """模型拒绝了这次请求（stop_reason=refusal）。"""


class InvalidModelOutput(RuntimeError):
    """重试若干次后仍拿不到合法 JSON。"""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


@dataclass
class BudgetTracker:
    """跑批期间的花费闸门。超预算直接抛异常，不是「尽量少花」。"""

    limit_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    usage: Usage = field(default_factory=Usage)

    def check(self, label: str = "") -> None:
        if self.limit_usd > 0 and self.spent_usd >= self.limit_usd:
            raise BudgetExceeded(
                f"已花费 ${self.spent_usd:.2f}，达到上限 ${self.limit_usd:.2f}"
                + (f"（停在 {label}）" if label else "")
            )

    def record(self, usage: Usage) -> None:
        self.spent_usd += usage.cost_usd
        self.calls += 1
        self.usage = self.usage + usage

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.spent_usd) if self.limit_usd > 0 else 0.0


class StructuredLLM(Protocol):
    """流水线只依赖这个接口，测试时可以塞假实现。"""

    def structured(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        label: str = "",
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], Usage]:
        ...


class AIClient:
    """封装 client.messages.create，只暴露「给我一个符合 schema 的 dict」。"""

    def __init__(
        self,
        config: Config,
        budget: BudgetTracker | None = None,
        client: Any | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.config = config
        self.budget = budget or BudgetTracker(limit_usd=config.nightly_budget_usd)
        self.max_attempts = max_attempts
        self._use_fallbacks = True
        if client is not None:
            self._client = client
        else:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover
                raise ImportError("需要先安装 SDK：pip install anthropic") from exc
            if not config.anthropic_api_key:
                # 不传 key 时 SDK 也会去找 ANTHROPIC_AUTH_TOKEN / ant auth 配置文件，
                # 所以这里不硬性报错，让 SDK 自己解析凭据。
                self._client = anthropic.Anthropic()
            else:
                self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    # ------------------------------------------------------------------ #
    def _price(self, usage: Any) -> Usage:
        cfg = self.config
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cost = (
            input_tokens * cfg.price_input_per_mtok
            + output_tokens * cfg.price_output_per_mtok
            + cache_read * cfg.price_cache_read_per_mtok
            + cache_write * cfg.price_cache_write_per_mtok
        ) / 1_000_000
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            cost_usd=cost,
        )

    def _create(
        self, system: str, user: str, schema: dict[str, Any], max_tokens: int
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    # 系统提示词每次都一样，缓存下来省钱
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self.config.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }
        if self._use_fallbacks:
            try:
                return self._client.beta.messages.create(
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                    **kwargs,
                )
            except TypeError:
                # SDK 版本还没有 fallbacks 参数，改走 extra_body
                try:
                    return self._client.beta.messages.create(
                        betas=[FALLBACK_BETA],
                        extra_body={"fallbacks": "default"},
                        **kwargs,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("fallbacks 不可用，降级为普通调用：%s", exc)
                    self._use_fallbacks = False
            except Exception as exc:  # noqa: BLE001
                if _is_bad_request(exc):
                    logger.warning("服务端拒绝 fallbacks，降级为普通调用：%s", exc)
                    self._use_fallbacks = False
                else:
                    raise
        return self._client.messages.create(**kwargs)

    # ------------------------------------------------------------------ #
    def structured(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        label: str = "",
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], Usage]:
        """调模型并返回 (解析好的 dict, 用量)。预算不够时抛 BudgetExceeded。"""
        self.budget.check(label)
        tokens = max_tokens or self.config.max_tokens
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._create(system, user, schema, tokens)
            except Exception as exc:  # noqa: BLE001 — SDK 内部已重试过 429/5xx
                last_error = exc
                if attempt >= self.max_attempts or not _is_retryable(exc):
                    raise
                sleep_for = min(2 ** attempt, 30)
                logger.warning(
                    "%s 第 %d 次调用失败（%s），%.0fs 后重试", label, attempt, exc, sleep_for
                )
                time.sleep(sleep_for)
                continue

            usage = self._price(getattr(response, "usage", None))
            self.budget.record(usage)

            stop_reason = getattr(response, "stop_reason", None)
            if stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None)
                raise ModelRefusal(f"模型拒绝了请求（category={category}）")
            if stop_reason == "max_tokens":
                logger.warning("%s 输出被 max_tokens 截断，本次结果可能不完整", label)

            text = _first_text(response)
            try:
                data = json.loads(text)
            except (ValueError, TypeError) as exc:
                last_error = exc
                logger.warning("%s 返回的不是合法 JSON（第 %d 次）", label, attempt)
                if attempt >= self.max_attempts:
                    break
                tokens = min(int(tokens * 1.5), 32000)
                continue

            if not isinstance(data, dict):
                last_error = InvalidModelOutput(f"顶层不是 object：{type(data)}")
                continue
            return data, usage

        raise InvalidModelOutput(
            f"{label}: {self.max_attempts} 次尝试后仍未拿到合法 JSON"
        ) from last_error


def _first_text(response: Any) -> str:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return exc.__class__.__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
    }


def _is_bad_request(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    return status == 400 or exc.__class__.__name__ == "BadRequestError"
