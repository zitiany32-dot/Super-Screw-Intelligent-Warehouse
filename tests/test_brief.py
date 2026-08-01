from __future__ import annotations

from datetime import date

from conftest import FakeCrawler

from boltmind import brief as brief_mod
from boltmind.pipeline import run_night
from boltmind.sources.demo import DemoCustomsSource

TODAY = date(2026, 8, 1)


def _run(conn, config, fake_llm, limit=2):
    return run_night(
        conn, config,
        sources=[DemoCustomsSource(limit=limit, today=TODAY)],
        llm=fake_llm, crawler=FakeCrawler(), today=TODAY,
    )[0]


def test_brief_reports_counts_and_drafts(conn, config, fake_llm):
    run_id = _run(conn, config, fake_llm)
    data = brief_mod.build_brief(conn, run_id)

    assert data["run"]["id"] == run_id
    assert len(data["items"]) == 2
    assert all(item["draft"] for item in data["items"])

    markdown = brief_mod.render_markdown(data)
    assert "昨晚处理了 2 家公司" in markdown
    assert "高优先级" in markdown
    assert "为什么是这个优先级" in markdown
    assert "所有草稿都停在 `pending` 状态" in markdown


def test_brief_html_escapes_content(conn, config, fake_llm, fake_analysis):
    fake_llm.responses["analyze:"] = {
        **fake_analysis,
        "reasons": "<script>alert('xss')</script> 品类对口",
    }
    run_id = _run(conn, config, fake_llm, limit=1)
    html = brief_mod.render_html(brief_mod.build_brief(conn, run_id))

    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "<!doctype html>" in html.lower()


def test_write_brief_creates_both_files(conn, config, fake_llm, tmp_path):
    run_id = _run(conn, config, fake_llm, limit=1)
    md_path, html_path, text = brief_mod.write_brief(conn, tmp_path / "briefs", run_id)
    assert md_path.exists() and html_path.exists()
    assert text.startswith("# BoltMind 早报")
    assert "<html" in html_path.read_text(encoding="utf-8").lower()


def test_brief_without_any_run(conn):
    data = brief_mod.build_brief(conn)
    assert data["run"] is None
    assert "还没有任何跑批记录" in brief_mod.render_markdown(data)
    assert "还没有任何跑批记录" in brief_mod.render_html(data)


def test_brief_flags_missing_recipients(conn, config, fake_analysis, fake_email):
    from conftest import FakeLLM

    from boltmind.enrich.website import CrawlResult

    llm = FakeLLM(
        {
            "analyze:": {**fake_analysis, "recommended_contact": "unknown"},
            "draft:": fake_email,
        }
    )
    run_night(
        conn, config,
        sources=[DemoCustomsSource(limit=1, today=TODAY)],
        llm=llm,
        crawler=FakeCrawler(CrawlResult(status="no_site", error="没有官网")),
        today=TODAY,
    )
    markdown = brief_mod.render_markdown(brief_mod.build_brief(conn))
    assert "收件人待补" in markdown
