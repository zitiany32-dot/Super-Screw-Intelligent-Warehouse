"""夜间流水线：抓线索 → 爬官网 → AI 分析 → 生成草稿 → 停在人工审核前。

这个模块永远不会发出任何一封邮件。它的终点是数据库里一批 status='pending'
的草稿，扣扳机的动作在 mailer.send_draft()，由人在后台点。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

from . import store
from .ai.analyze import analyze_company, draft_cold_email, real_people
from .ai.client import AIClient, BudgetExceeded, BudgetTracker, ModelRefusal, StructuredLLM
from .config import Config
from .db import utcnow
from .discover import DiscoveryContext, DiscoveryResult, discover_emails
from .enrich.website import WebsiteCrawler
from .scoring import prescore, priority_from_score
from .sources.base import CustomsLead

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}$")


@dataclass
class RunStats:
    leads_seen: int = 0
    companies_new: int = 0
    companies_touched: int = 0
    records_new: int = 0
    crawled_ok: int = 0
    crawled_failed: int = 0
    analyzed: int = 0
    drafts_created: int = 0
    high_priority: int = 0
    medium_priority: int = 0
    low_priority: int = 0
    emails_found: int = 0
    companies_with_email: int = 0
    skipped_no_contact: int = 0
    errors: list[str] = field(default_factory=list)
    budget_stopped: bool = False
    cost_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "leads_seen": self.leads_seen,
            "companies_new": self.companies_new,
            "companies_touched": self.companies_touched,
            "records_new": self.records_new,
            "crawled_ok": self.crawled_ok,
            "crawled_failed": self.crawled_failed,
            "analyzed": self.analyzed,
            "drafts_created": self.drafts_created,
            "high_priority": self.high_priority,
            "medium_priority": self.medium_priority,
            "low_priority": self.low_priority,
            "emails_found": self.emails_found,
            "companies_with_email": self.companies_with_email,
            "skipped_no_contact": self.skipped_no_contact,
            "budget_stopped": self.budget_stopped,
            "cost_usd": round(self.cost_usd, 4),
            "errors": self.errors[:20],
        }


# --------------------------------------------------------------------------- #
# 第一步：入库
# --------------------------------------------------------------------------- #

def ingest(
    conn: sqlite3.Connection, leads: Iterable[CustomsLead], stats: RunStats | None = None
) -> RunStats:
    stats = stats or RunStats()
    for lead in leads:
        stats.leads_seen += 1
        try:
            existed = store.find_company_id(conn, lead.company_name)
            company_id = store.upsert_company(conn, lead.company_dict())
            if existed is None:
                stats.companies_new += 1
            if store.add_customs_record(conn, company_id, lead.record_dict()):
                stats.records_new += 1
        except Exception as exc:  # noqa: BLE001 — 单条脏数据不该拖垮整批
            logger.warning("入库失败 %s: %s", lead.company_name, exc)
            stats.errors.append(f"ingest[{lead.company_name}]: {exc}")
    conn.commit()
    return stats


# --------------------------------------------------------------------------- #
# 第二步：挑今晚要处理的公司
# --------------------------------------------------------------------------- #

def select_candidates(
    conn: sqlite3.Connection, limit: int, today: date | None = None
) -> list[dict[str, Any]]:
    """选出还没分析过的公司，按预打分从高到低取前 N 家。"""
    rows = conn.execute(
        """
        SELECT c.* FROM companies c
        LEFT JOIN analyses a ON a.company_id = c.id
        WHERE a.id IS NULL AND c.status NOT IN ('skipped', 'contacted', 'replied')
        GROUP BY c.id
        """
    ).fetchall()

    scored: list[tuple[int, list[str], dict[str, Any]]] = []
    for row in rows:
        company = dict(row)
        records = store.get_customs_records(conn, company["id"])
        value, reasons = prescore(company, records, today=today)
        scored.append((value, reasons, company))

    scored.sort(key=lambda item: -item[0])
    selected = []
    for value, reasons, company in scored[:limit]:
        company["_prescore"] = value
        company["_prescore_reasons"] = reasons
        selected.append(company)
    return selected


# --------------------------------------------------------------------------- #
# 第三步：爬官网
# --------------------------------------------------------------------------- #

def enrich_company(
    conn: sqlite3.Connection,
    crawler: WebsiteCrawler,
    company: dict[str, Any],
    stats: RunStats,
) -> dict[str, Any] | None:
    existing = store.get_enrichment(conn, company["id"])
    if existing and existing.get("status") == "ok":
        return existing

    result = crawler.crawl(company.get("website"), company.get("domain"))
    store.save_enrichment(conn, company["id"], result.as_dict())

    if result.status == "ok":
        stats.crawled_ok += 1
        # 官网上找到的邮箱回填到公司表，后面发信要用
        if not company.get("contact_email") and result.emails:
            best = pick_best_email(result.emails, company.get("domain"))
            if best:
                conn.execute(
                    "UPDATE companies SET contact_email = ? WHERE id = ?",
                    (best, company["id"]),
                )
                company["contact_email"] = best
        if result.homepage_url and not company.get("website"):
            conn.execute(
                "UPDATE companies SET website = ? WHERE id = ?",
                (result.homepage_url, company["id"]),
            )
            company["website"] = result.homepage_url
    else:
        stats.crawled_failed += 1

    store.set_company_status(conn, company["id"], "enriched")
    conn.commit()
    return store.get_enrichment(conn, company["id"])


# 销售/询价类邮箱优先，hr/招聘/法务往后排
_EMAIL_PREFERENCE = (
    "sales", "info", "contact", "enquiry", "inquiry", "commercial", "purchas",
    "procurement", "office", "hello", "ventas", "vertrieb", "compras",
)
_EMAIL_DEPRIORITIZE = ("hr", "jobs", "career", "recruit", "legal", "privacy", "abuse", "press")


def pick_best_email(emails: list[str], domain: str | None = None) -> str | None:
    """在抓到的一堆邮箱里挑最像业务联系人的那个。挑不出就返回 None。"""
    candidates = [e.lower().strip() for e in emails if _EMAIL_RE.match(e.strip())]
    if not candidates:
        return None

    def score(email: str) -> int:
        local, _, host = email.partition("@")
        value = 0
        if domain and domain.lower().lstrip("www.") in host:
            value += 10  # 同域名的才可信
        for i, word in enumerate(_EMAIL_PREFERENCE):
            if word in local:
                value += 8 - i // 3
                break
        if any(word in local for word in _EMAIL_DEPRIORITIZE):
            value -= 10
        return value

    best = max(candidates, key=score)
    return best if score(best) > 0 else candidates[0]


# --------------------------------------------------------------------------- #
# 第四步：多源邮箱发现
# --------------------------------------------------------------------------- #

DiscoverFn = Any  # (DiscoveryContext, Config) -> DiscoveryResult


def discover_company_emails(
    conn: sqlite3.Connection,
    config: Config,
    company: dict[str, Any],
    enrichment: dict[str, Any] | None,
    analysis: dict[str, Any],
    stats: RunStats,
    discover_fn: DiscoverFn | None = None,
) -> DiscoveryResult:
    """跑一遍多源邮箱发现，存候选、回填联系人。返回排好序的候选。"""
    context = DiscoveryContext(
        company_name=company.get("name") or "",
        domain=company.get("domain"),
        website=company.get("website"),
        country=company.get("country"),
        known_emails=list((enrichment or {}).get("emails") or []),
        site_text=(enrichment or {}).get("text"),
        people=real_people(analysis),
    )
    fn = discover_fn or discover_emails
    try:
        result = fn(context, config)
    except Exception as exc:  # noqa: BLE001 — 发现环节挂了不该拖垮整家公司
        logger.info("邮箱发现失败 %s: %s", company.get("name"), exc)
        return DiscoveryResult(candidates=[])

    if result.candidates:
        store.save_email_candidates(
            conn, company["id"], [c.as_dict() for c in result.candidates]
        )
        stats.emails_found += len(result.candidates)
        stats.companies_with_email += 1

    usable = result.usable()
    if usable and not company.get("contact_email"):
        conn.execute(
            "UPDATE companies SET contact_email = ? WHERE id = ?",
            (usable.email, company["id"]),
        )
        company["contact_email"] = usable.email
    conn.commit()
    return result


# --------------------------------------------------------------------------- #
# 第五步：确定收件人 + 起草
# --------------------------------------------------------------------------- #

def resolve_recipient(
    analysis: dict[str, Any],
    company: dict[str, Any],
    enrichment: dict[str, Any] | None,
    discovery: DiscoveryResult | None = None,
) -> tuple[str | None, str | None]:
    """确定收件人，返回 (邮箱, 提示)。

    所有候选都来自真实来源（官网/WHOIS/搜索/Hunter/人名模式），没有一个是模型
    凭空编的。模型推荐的邮箱只有在候选集里出现过才采信。
    """
    if discovery and discovery.candidates:
        recommended = str(analysis.get("recommended_contact") or "").strip().lower()
        by_email = {c.email: c for c in discovery.candidates}
        if _EMAIL_RE.match(recommended) and recommended in by_email:
            chosen = by_email[recommended]
        else:
            chosen = discovery.usable() or discovery.best
        if chosen:
            hint = None
            if chosen.source == "pattern" and chosen.verified not in {"smtp_ok"}:
                hint = f"⚠️ 收件人为推测邮箱（{chosen.source}/{chosen.verified}），发送前务必核实"
            elif chosen.confidence < 45:
                hint = f"⚠️ 收件人可信度偏低（{chosen.confidence}），建议人工确认"
            return chosen.email, hint

    if company.get("contact_email"):
        return str(company["contact_email"]).lower(), None
    known = [e for e in (enrichment or {}).get("emails", []) if e]
    return (pick_best_email(known, company.get("domain")), None) if known else (None, None)


def process_company(
    conn: sqlite3.Connection,
    config: Config,
    llm: StructuredLLM,
    company: dict[str, Any],
    enrichment: dict[str, Any] | None,
    run_id: int,
    stats: RunStats,
    discover_fn: DiscoverFn | None = None,
) -> None:
    records = store.get_customs_records(conn, company["id"])
    prescore_value = company.get("_prescore")
    prescore_reasons = company.get("_prescore_reasons")
    if prescore_value is None:
        prescore_value, prescore_reasons = prescore(company, records)

    analysis_data, usage = analyze_company(
        llm, config.seller, company, records, enrichment,
        prescore_value, prescore_reasons or [],
    )
    stats.analyzed += 1
    stats.cost_usd += usage.cost_usd

    priority = analysis_data["priority"]
    # 规则分和模型分差太多时以规则分为准兜底，避免模型把明显的低质线索抬高
    if priority == "high" and prescore_value < 30:
        priority = "medium"
        analysis_data["risks"] = (
            (analysis_data.get("risks") or "") + " | 规则预打分偏低，已下调优先级"
        ).strip(" |")
    analysis_data["priority"] = priority

    analysis_id = store.save_analysis(
        conn,
        company["id"],
        {
            "run_id": run_id,
            "model": config.model,
            "priority": priority,
            "score": analysis_data["score"],
            "prescore": prescore_value,
            "profile": analysis_data.get("profile"),
            "assessment": analysis_data.get("assessment"),
            "contacts": analysis_data.get("contacts_found"),
            "angles": analysis_data.get("angles"),
            "reasons": analysis_data.get("reasons"),
            "risks": analysis_data.get("risks"),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd,
        },
    )
    counter = f"{priority}_priority"
    setattr(stats, counter, getattr(stats, counter) + 1)
    store.set_company_status(conn, company["id"], "analyzed")
    conn.commit()

    # 多源邮箱发现：分析拿到真实人名后再跑，好让人名模式派上用场
    discovery = discover_company_emails(
        conn, config, company, enrichment, analysis_data, stats, discover_fn
    )

    if priority == "low":
        logger.info("%s 优先级 low，不生成草稿", company["name"])
        return

    email_data, email_usage = draft_cold_email(
        llm, config.seller, company, analysis_data, records, enrichment
    )
    stats.cost_usd += email_usage.cost_usd

    recipient, recipient_hint = resolve_recipient(
        analysis_data, company, enrichment, discovery
    )
    if not recipient:
        stats.skipped_no_contact += 1

    best = discovery.best
    email_source = (
        f"{best.source}/{best.verified}（可信度 {best.confidence}）" if best else "—"
    )
    rationale_parts = [
        f"优先级: {priority}（模型 {analysis_data['score']} / 规则 {prescore_value}）",
        f"理由: {analysis_data.get('reasons') or '—'}",
        f"收件人来源: {email_source}",
        f"写信思路: {email_data.get('rationale') or '—'}",
        f"引用的事实: {email_data.get('personalization_used') or '—'}",
        f"需人工确认: {email_data.get('review_flags') or '无'}",
    ]
    if recipient_hint:
        rationale_parts.append(recipient_hint)
    if not recipient:
        rationale_parts.append("⚠️ 没有找到任何可信邮箱，发送前需人工补上")

    store.save_draft(
        conn,
        {
            "company_id": company["id"],
            "analysis_id": analysis_id,
            "run_id": run_id,
            "kind": "cold",
            "to_email": recipient,
            "language": email_data.get("language"),
            "subject": email_data.get("subject"),
            "body": email_data.get("body"),
            "rationale": "\n".join(rationale_parts),
        },
    )
    stats.drafts_created += 1
    store.set_company_status(conn, company["id"], "drafted")
    conn.commit()


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def run_night(
    conn: sqlite3.Connection,
    config: Config,
    sources: Iterable[Any],
    llm: StructuredLLM | None = None,
    crawler: WebsiteCrawler | None = None,
    today: date | None = None,
    discover_fn: DiscoverFn | None = None,
) -> tuple[int, RunStats]:
    """跑完整的一晚。返回 (run_id, 统计)。"""
    run_id = store.start_run(conn)
    conn.commit()
    stats = RunStats()

    budget = BudgetTracker(limit_usd=config.nightly_budget_usd)
    llm = llm or AIClient(config, budget=budget)
    crawler = crawler or WebsiteCrawler(
        user_agent=config.user_agent,
        timeout=config.crawl_timeout,
        max_pages=config.crawl_max_pages,
        delay=config.crawl_delay_seconds,
        respect_robots=config.respect_robots,
    )

    status = "ok"
    error: str | None = None
    try:
        for source in sources:
            logger.info("读取数据源: %s", getattr(source, "name", source))
            ingest(conn, source.fetch(), stats)

        candidates = select_candidates(conn, config.max_companies_per_night, today)
        logger.info("本轮候选公司 %d 家", len(candidates))

        for company in candidates:
            stats.companies_touched += 1
            try:
                enrichment = enrich_company(conn, crawler, company, stats)
                process_company(
                    conn, config, llm, company, enrichment, run_id, stats,
                    discover_fn=discover_fn,
                )
            except BudgetExceeded as exc:
                logger.warning("预算用尽，本轮提前结束：%s", exc)
                stats.budget_stopped = True
                status = "partial"
                error = str(exc)
                break
            except ModelRefusal as exc:
                logger.warning("模型拒绝处理 %s：%s", company["name"], exc)
                stats.errors.append(f"refusal[{company['name']}]: {exc}")
            except Exception as exc:  # noqa: BLE001 — 一家出错不影响其余
                logger.exception("处理 %s 时出错", company["name"])
                stats.errors.append(f"process[{company['name']}]: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("流水线异常终止")
        status = "failed"
        error = str(exc)
    finally:
        if stats.errors and status == "ok":
            status = "ok_with_errors"
        store.finish_run(conn, run_id, stats.as_dict(), stats.cost_usd, status, error)
        conn.commit()

    return run_id, stats


def default_sources(config: Config, use_demo: bool = False) -> list[Any]:
    """默认数据源：data/inbox/ 下的导出文件；没有文件时可退回演示数据。"""
    from .sources.csv_source import CsvCustomsSource
    from .sources.demo import DemoCustomsSource

    inbox = config.data_dir / "inbox"
    has_files = inbox.exists() and any(
        p.suffix.lower() in {".csv", ".tsv", ".xlsx", ".xlsm"} for p in inbox.iterdir()
    )
    if has_files:
        return [CsvCustomsSource(inbox)]
    if use_demo:
        logger.warning("data/inbox/ 里没有数据文件，使用演示数据（虚构）")
        return [DemoCustomsSource()]
    logger.warning("data/inbox/ 里没有数据文件，本轮无新线索")
    return []


__all__ = [
    "RunStats",
    "ingest",
    "select_candidates",
    "enrich_company",
    "process_company",
    "run_night",
    "default_sources",
    "pick_best_email",
    "resolve_recipient",
    "utcnow",
]
