from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from customsradar.ai.client import Usage  # noqa: E402
from customsradar.config import Config, SellerProfile  # noqa: E402
from customsradar.db import init_db  # noqa: E402
from customsradar.enrich.website import CrawlResult  # noqa: E402


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path,
        db_path=tmp_path / "test.db",
        anthropic_api_key="test-key",
        nightly_budget_usd=1.0,
        max_companies_per_night=5,
        crawl_delay_seconds=0.0,
        # 测试里关掉所有联网的邮箱发现，只留官网+人名模式两个离线来源
        discover_whois=False,
        verify_mx=False,
        verify_smtp=False,
        smtp_host="smtp.example.com",
        smtp_from="sales@example.com",
        allow_send=False,
        seller=SellerProfile(sender_name="Li Wei", sender_email="sales@example.com"),
    )


@pytest.fixture()
def conn(config: Config):
    connection = init_db(config.db_path)
    yield connection
    connection.close()


class FakeLLM:
    """按 label 前缀返回预置结果，记录所有调用，供断言用。"""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict[str, Any]] = []
        self.usage = Usage(input_tokens=1000, output_tokens=500, cost_usd=0.0175)

    def structured(self, *, system, user, schema, label="", max_tokens=None):
        self.calls.append(
            {"system": system, "user": user, "schema": schema, "label": label}
        )
        for prefix, payload in self.responses.items():
            if label.startswith(prefix):
                return dict(payload), self.usage
        raise AssertionError(f"FakeLLM 没有为 label={label!r} 预置返回值")


class FakeCrawler:
    def __init__(self, result: CrawlResult | None = None) -> None:
        self.result = result or CrawlResult(
            status="ok",
            homepage_url="https://buyer.example.com",
            pages=[{"url": "https://buyer.example.com", "title": "Buyer", "chars": 400}],
            text="We distribute industrial fasteners across Europe.",
            emails=["sales@buyer.example.com"],
            phones=[],
        )
        self.calls: list[tuple[str | None, str | None]] = []

    def crawl(self, website, domain=None):
        self.calls.append((website, domain))
        return self.result


@pytest.fixture()
def fake_analysis() -> dict[str, Any]:
    return {
        "profile": {
            "business_summary": "欧洲工业紧固件分销商。",
            "likely_role": "distributor",
            "products_focus": "螺栓螺母",
            "size_signal": "中型",
            "buying_pattern": "每季度从中国采购",
            "decision_maker_guess": "采购经理",
            "website_language": "en",
        },
        "angles": [
            {
                "angle": "他们在采购 8.8 级六角螺栓，正是我们的主力产品",
                "evidence": "2024-05 提单 HS 731815",
                "opening_line": "I noticed you import grade 8.8 hex bolts.",
            }
        ],
        "priority": "high",
        "score": 82,
        "reasons": "品类对口且近期活跃。",
        "risks": "无",
        "recommended_contact": "sales@buyer.example.com",
        "email_language": "en",
        "data_gaps": "无",
    }


@pytest.fixture()
def fake_email() -> dict[str, Any]:
    return {
        "subject": "Grade 8.8 hex bolts from a Yongnian factory",
        "body": "Hello,\n\nI saw that you import grade 8.8 hex bolts.\n\nBest regards",
        "language": "en",
        "personalization_used": "提单 HS 731815",
        "rationale": "用提单事实开头。",
        "review_flags": "无",
    }


@pytest.fixture()
def fake_llm(fake_analysis, fake_email) -> FakeLLM:
    return FakeLLM({"analyze:": fake_analysis, "draft:": fake_email})
