from __future__ import annotations

from datetime import date

from conftest import FakeCrawler, FakeLLM

from customsradar import store
from customsradar.ai.client import BudgetExceeded
from customsradar.enrich.website import CrawlResult
from customsradar.pipeline import (
    RunStats,
    ingest,
    pick_best_email,
    resolve_recipient,
    run_night,
    select_candidates,
)
from customsradar.sources.demo import DemoCustomsSource

TODAY = date(2026, 8, 1)


def test_ingest_dedupes_companies_and_records(conn):
    source = DemoCustomsSource(limit=3, today=TODAY)
    stats = ingest(conn, source.fetch())
    assert stats.companies_new == 3
    assert stats.records_new == stats.leads_seen

    # 再跑一遍同样的数据，不该产生任何新增
    again = ingest(conn, DemoCustomsSource(limit=3, today=TODAY).fetch())
    assert again.companies_new == 0
    assert again.records_new == 0


def test_ingest_survives_a_bad_lead(conn):
    from customsradar.sources.base import CustomsLead

    stats = ingest(
        conn,
        [
            CustomsLead(company_name=""),          # 没名字 → 报错但不中断
            CustomsLead(company_name="Good Co"),
        ],
    )
    assert stats.companies_new == 1
    assert stats.errors


def test_select_candidates_ranks_by_prescore(conn):
    ingest(conn, DemoCustomsSource(today=TODAY).fetch())
    candidates = select_candidates(conn, limit=3, today=TODAY)
    assert len(candidates) == 3
    scores = [c["_prescore"] for c in candidates]
    assert scores == sorted(scores, reverse=True)
    assert all(c["_prescore_reasons"] for c in candidates)


def test_select_candidates_skips_already_analyzed(conn):
    ingest(conn, DemoCustomsSource(limit=2, today=TODAY).fetch())
    first = select_candidates(conn, limit=1, today=TODAY)[0]
    store.save_analysis(conn, first["id"], {"priority": "high", "score": 80})
    conn.commit()
    remaining = [c["id"] for c in select_candidates(conn, limit=10, today=TODAY)]
    assert first["id"] not in remaining


def test_pick_best_email_prefers_sales_over_hr():
    emails = ["hr@acme.example.com", "sales@acme.example.com", "webmaster@other.example"]
    assert pick_best_email(emails, "acme.example.com") == "sales@acme.example.com"


def test_pick_best_email_returns_none_for_garbage():
    assert pick_best_email(["not-an-email", "@@"], "acme.example.com") is None
    assert pick_best_email([], None) is None


def _candidate(email, source="website", confidence=80, verified="mx"):
    from customsradar.discover import EmailCandidate

    return EmailCandidate(
        email=email, source=source, confidence=confidence, verified=verified
    )


def test_resolve_recipient_rejects_invented_email():
    """模型推荐的邮箱如果不在候选集里，一律不采信 —— 防止发信到编出来的地址。"""
    from customsradar.discover import DiscoveryResult

    analysis = {"recommended_contact": "ceo@totally-made-up.example.com"}
    discovery = DiscoveryResult(candidates=[_candidate("info@acme.example.com")])
    email, hint = resolve_recipient(analysis, {"domain": "acme.example.com"}, None, discovery)
    assert email == "info@acme.example.com"


def test_resolve_recipient_accepts_recommended_when_in_candidates():
    from customsradar.discover import DiscoveryResult

    analysis = {"recommended_contact": "sales@acme.example.com"}
    discovery = DiscoveryResult(
        candidates=[
            _candidate("info@acme.example.com", confidence=70),
            _candidate("sales@acme.example.com", confidence=65),
        ]
    )
    email, _ = resolve_recipient(analysis, {"domain": "acme.example.com"}, None, discovery)
    assert email == "sales@acme.example.com"


def test_resolve_recipient_flags_pattern_guess():
    from customsradar.discover import DiscoveryResult

    discovery = DiscoveryResult(
        candidates=[_candidate("j.smith@acme.example.com", source="pattern",
                               confidence=40, verified="mx")]
    )
    email, hint = resolve_recipient({}, {"domain": "acme.example.com"}, None, discovery)
    assert email == "j.smith@acme.example.com"
    assert hint and "核实" in hint


def test_resolve_recipient_none_when_nothing_found():
    from customsradar.discover import DiscoveryResult

    email, hint = resolve_recipient({}, {}, None, DiscoveryResult(candidates=[]))
    assert email is None


def test_run_night_end_to_end_stops_before_sending(conn, config, fake_llm):
    config.max_companies_per_night = 3
    crawler = FakeCrawler()
    run_id, stats = run_night(
        conn,
        config,
        sources=[DemoCustomsSource(limit=4, today=TODAY)],
        llm=fake_llm,
        crawler=crawler,
        today=TODAY,
    )

    assert stats.analyzed == 3
    assert stats.drafts_created == 3
    assert stats.high_priority == 3
    assert stats.cost_usd > 0

    drafts = store.list_drafts(conn, status="pending")
    assert len(drafts) == 3
    # 关键不变量：流水线跑完，没有任何一封信被发出去
    assert all(d["status"] == "pending" for d in drafts)
    assert all(d["sent_at"] is None for d in drafts)
    assert store.list_drafts(conn, status="sent") == []

    run = store.get_run(conn, run_id)
    assert run["status"] == "ok"
    assert run["stats"]["drafts_created"] == 3


def test_run_night_skips_draft_for_low_priority(conn, config, fake_analysis, fake_email):
    low = {**fake_analysis, "priority": "low", "score": 15}
    llm = FakeLLM({"analyze:": low, "draft:": fake_email})
    _, stats = run_night(
        conn, config,
        sources=[DemoCustomsSource(limit=2, today=TODAY)],
        llm=llm, crawler=FakeCrawler(), today=TODAY,
    )
    assert stats.low_priority > 0
    assert stats.drafts_created == 0
    assert not any(c["label"].startswith("draft:") for c in llm.calls)


def test_high_priority_is_downgraded_when_prescore_is_low(conn, config, fake_analysis, fake_email):
    """模型说 high 但规则分很低时下调 —— 防止模型把垃圾线索抬上去。"""
    from customsradar.sources.base import CustomsLead

    llm = FakeLLM({"analyze:": fake_analysis, "draft:": fake_email})
    ingest(conn, [CustomsLead(company_name="Mystery Trading", product_desc="TEXTILES")])
    run_night(conn, config, sources=[], llm=llm, crawler=FakeCrawler(), today=TODAY)

    company = store.list_companies(conn)[0]
    analysis = store.latest_analysis(conn, company["id"])
    assert analysis["prescore"] < 30
    assert analysis["priority"] == "medium"
    assert "下调优先级" in (analysis["risks"] or "")


def test_run_night_stops_cleanly_when_budget_runs_out(conn, config, fake_analysis):
    class BrokeLLM(FakeLLM):
        def structured(self, **kwargs):
            raise BudgetExceeded("预算用完了")

    _, stats = run_night(
        conn, config,
        sources=[DemoCustomsSource(limit=3, today=TODAY)],
        llm=BrokeLLM(), crawler=FakeCrawler(), today=TODAY,
    )
    assert stats.budget_stopped is True
    assert stats.drafts_created == 0
    assert store.latest_run(conn)["status"] == "partial"


def test_run_night_continues_after_one_company_fails(conn, config, fake_analysis, fake_email):
    class FlakyLLM(FakeLLM):
        def __init__(self):
            super().__init__({"analyze:": fake_analysis, "draft:": fake_email})
            self.seen = 0

        def structured(self, **kwargs):
            if kwargs.get("label", "").startswith("analyze:"):
                self.seen += 1
                if self.seen == 1:
                    raise RuntimeError("模拟单家公司分析失败")
            return super().structured(**kwargs)

    _, stats = run_night(
        conn, config,
        sources=[DemoCustomsSource(limit=3, today=TODAY)],
        llm=FlakyLLM(), crawler=FakeCrawler(), today=TODAY,
    )
    assert stats.errors
    assert stats.analyzed >= 1
    assert store.latest_run(conn)["status"] == "ok_with_errors"


def test_failed_crawl_still_produces_analysis(conn, config, fake_llm):
    crawler = FakeCrawler(CrawlResult(status="no_site", error="没有官网或域名"))
    _, stats = run_night(
        conn, config,
        sources=[DemoCustomsSource(limit=1, today=TODAY)],
        llm=fake_llm, crawler=crawler, today=TODAY,
    )
    assert stats.crawled_failed == 1
    assert stats.analyzed == 1


def test_crawled_email_is_backfilled_to_company(conn, config, fake_llm):
    run_night(
        conn, config,
        sources=[DemoCustomsSource(limit=1, today=TODAY)],
        llm=fake_llm, crawler=FakeCrawler(), today=TODAY,
    )
    company = store.list_companies(conn)[0]
    assert company["contact_email"] == "sales@buyer.example.com"


def test_run_stats_serializes():
    stats = RunStats(analyzed=2, errors=["boom"])
    payload = stats.as_dict()
    assert payload["analyzed"] == 2
    assert payload["errors"] == ["boom"]
