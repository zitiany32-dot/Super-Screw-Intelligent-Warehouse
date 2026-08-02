"""收信：拉 IMAP → 匹配到之前发出去的信 → AI 分析意图 → 起草回信（仍然只存草稿）。

用 BODY.PEEK 取信，不改邮箱里的已读状态；靠 Message-ID 去重，重复跑不会重复处理。
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any

from . import store
from .ai.analyze import analyze_and_draft_reply
from .ai.client import AIClient, BudgetExceeded, BudgetTracker, StructuredLLM
from .config import Config
from .db import utcnow

logger = logging.getLogger(__name__)

_MSGID_RE = re.compile(r"<[^<>@\s]+@[^<>\s]+>")
# 明确不该自动起草回信的意图
_NO_REPLY_INTENTS = {"out_of_office", "spam_or_unrelated", "unsubscribe"}


@dataclass
class InboxStats:
    fetched: int = 0
    new_replies: int = 0
    matched: int = 0
    unmatched: int = 0
    analyzed: int = 0
    reply_drafts: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "new_replies": self.new_replies,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "analyzed": self.analyzed,
            "reply_drafts": self.reply_drafts,
            "cost_usd": round(self.cost_usd, 4),
            "errors": self.errors[:20],
        }


# --------------------------------------------------------------------------- #
# 邮件解析
# --------------------------------------------------------------------------- #

def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 — 编码乱七八糟的邮件头
        return value


def extract_body(message: Message, max_chars: int = 20_000) -> str:
    """优先取 text/plain；只有 HTML 时降级成去标签的纯文本。"""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in message.walk() if message.is_multipart() else [message]:
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" in disposition.lower():
            continue
        if content_type not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        (plain_parts if content_type == "text/plain" else html_parts).append(text)

    if plain_parts:
        body = "\n".join(plain_parts)
    elif html_parts:
        from .enrich.extract import parse_html

        body = parse_html("\n".join(html_parts))["text"]
    else:
        body = ""
    return body.strip()[:max_chars]


def strip_quoted(body: str) -> str:
    """去掉引用的原文，只留对方新写的内容 —— 省 token，也让分析更准。"""
    lines = body.splitlines()
    cut = len(lines)
    patterns = (
        re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.I),
        re.compile(r"^\s*On .+ wrote:\s*$", re.I),
        re.compile(r"^\s*(From|发件人)\s*[:：]", re.I),
        re.compile(r"^\s*_{10,}\s*$"),
        re.compile(r"^\s*在\s*.+\s*写道[:：]"),
    )
    for i, line in enumerate(lines):
        if any(p.match(line) for p in patterns):
            cut = i
            break
    quoted_free = [ln for ln in lines[:cut] if not ln.lstrip().startswith(">")]
    trimmed = "\n".join(quoted_free).strip()
    return trimmed or body.strip()


def parse_message(raw: bytes) -> dict[str, Any]:
    message = email.message_from_bytes(raw)
    references = _MSGID_RE.findall(message.get("References", "") or "")
    in_reply_to = _MSGID_RE.findall(message.get("In-Reply-To", "") or "")
    received_at = utcnow()
    if message.get("Date"):
        try:
            received_at = parsedate_to_datetime(message["Date"]).isoformat()
        except (TypeError, ValueError):
            pass

    from_header = _decode(message.get("From"))
    from_email = ""
    match = re.search(r"[\w.+\-]+@[\w\-.]+\.\w+", from_header)
    if match:
        from_email = match.group().lower()

    body = strip_quoted(extract_body(message))
    return {
        "message_id": (_MSGID_RE.findall(message.get("Message-ID", "") or "") or [""])[0],
        "in_reply_to": (in_reply_to or references[-1:] or [""])[0],
        "references": references,
        "from_email": from_email,
        "from_name": from_header,
        "subject": _decode(message.get("Subject")),
        "received_at": received_at,
        "body": body,
        "auto_reply": bool(
            message.get("Auto-Submitted")
            or message.get("X-Autoreply")
            or message.get("X-Autorespond")
        ),
    }


# --------------------------------------------------------------------------- #
# 匹配到我们发出去的信
# --------------------------------------------------------------------------- #

def match_thread(
    conn: sqlite3.Connection, parsed: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """返回 (原始草稿, 公司)。先按 Message-ID 串线程，再退化到按发件邮箱找。"""
    candidates = [parsed.get("in_reply_to")] + list(parsed.get("references") or [])
    for message_id in [c for c in candidates if c]:
        draft = store.find_draft_by_message_id(conn, message_id)
        if draft:
            return draft, store.get_company(conn, draft["company_id"])

    from_email = parsed.get("from_email") or ""
    if from_email:
        row = conn.execute(
            "SELECT * FROM companies WHERE lower(contact_email) = ? LIMIT 1",
            (from_email,),
        ).fetchone()
        if row is None and "@" in from_email:
            domain = from_email.split("@")[1]
            row = conn.execute(
                "SELECT * FROM companies WHERE lower(domain) = ? LIMIT 1", (domain,)
            ).fetchone()
        if row is not None:
            company = dict(row)
            sent = conn.execute(
                """
                SELECT * FROM drafts WHERE company_id = ? AND status = 'sent'
                ORDER BY sent_at DESC LIMIT 1
                """,
                (company["id"],),
            ).fetchone()
            return (dict(sent) if sent else None), company
    return None, None


# --------------------------------------------------------------------------- #
# IMAP
# --------------------------------------------------------------------------- #

def fetch_messages(config: Config, since_days: int = 7, limit: int = 50) -> list[bytes]:
    if not config.imap_host:
        raise RuntimeError("没有配置 IMAP_HOST")

    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
    conn = imaplib.IMAP4_SSL(config.imap_host, config.imap_port)
    try:
        conn.login(config.imap_user, config.imap_password)
        conn.select(config.imap_folder, readonly=True)
        status, data = conn.search(None, f'(SINCE "{since}")')
        if status != "OK":
            return []
        uids = (data[0] or b"").split()
        raws: list[bytes] = []
        for uid in uids[-limit:]:
            status, payload = conn.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            for part in payload:
                if isinstance(part, tuple) and part[1]:
                    raws.append(part[1])
                    break
        return raws
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def process_replies(
    conn: sqlite3.Connection,
    config: Config,
    raw_messages: list[bytes],
    llm: StructuredLLM | None = None,
) -> InboxStats:
    """解析、入库、AI 分析、起草回信。回信同样只存草稿，不发送。"""
    stats = InboxStats()
    llm = llm or AIClient(
        config, budget=BudgetTracker(limit_usd=config.nightly_budget_usd)
    )

    for raw in raw_messages:
        stats.fetched += 1
        try:
            parsed = parse_message(raw)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"parse: {exc}")
            continue

        if not parsed.get("message_id"):
            continue
        if conn.execute(
            "SELECT 1 FROM replies WHERE message_id = ?", (parsed["message_id"],)
        ).fetchone():
            continue  # 处理过了

        original_draft, company = match_thread(conn, parsed)
        if company is None:
            stats.unmatched += 1
            continue  # 跟我们的外发无关，不入库、不花 AI 钱
        stats.matched += 1

        reply_id = store.save_reply(
            conn,
            {
                "company_id": company["id"],
                "draft_id": original_draft["id"] if original_draft else None,
                "received_at": parsed["received_at"],
                "from_email": parsed["from_email"],
                "subject": parsed["subject"],
                "body": parsed["body"],
                "message_id": parsed["message_id"],
                "in_reply_to": parsed["in_reply_to"],
            },
        )
        conn.commit()
        if reply_id is None:
            continue
        stats.new_replies += 1
        store.set_company_status(conn, company["id"], "replied")

        if parsed.get("auto_reply"):
            store.update_reply(
                conn, reply_id, intent="out_of_office", summary="自动回复，无需处理"
            )
            conn.commit()
            continue

        try:
            analysis = store.latest_analysis(conn, company["id"])
            result, usage = analyze_and_draft_reply(
                llm, config.seller, company, original_draft,
                {**parsed, "id": reply_id}, analysis,
            )
        except BudgetExceeded as exc:
            stats.errors.append(f"budget: {exc}")
            break
        except Exception as exc:  # noqa: BLE001
            logger.exception("分析回复 #%s 失败", reply_id)
            stats.errors.append(f"analyze[{reply_id}]: {exc}")
            continue

        stats.analyzed += 1
        stats.cost_usd += usage.cost_usd
        store.update_reply(
            conn,
            reply_id,
            intent=result.get("intent"),
            urgency=result.get("urgency"),
            summary=result.get("summary"),
            analysis=result,
        )

        reply_body = (result.get("reply_body") or "").strip()
        intent = result.get("intent")
        if reply_body and intent not in _NO_REPLY_INTENTS:
            rationale = "\n".join(
                [
                    f"意图: {intent} · 紧急度: {result.get('urgency')}",
                    f"对方说了什么: {result.get('summary') or '—'}",
                    f"关键诉求: {'; '.join(result.get('key_points') or []) or '—'}",
                    f"异议: {result.get('objections') or '无'}",
                    f"建议动作: {result.get('recommended_action') or '—'}",
                    f"需人工确认: {result.get('needs_human_input') or '无'}",
                ]
            )
            store.save_draft(
                conn,
                {
                    "company_id": company["id"],
                    "reply_id": reply_id,
                    "kind": "reply",
                    "to_email": parsed["from_email"],
                    "language": result.get("reply_language"),
                    "subject": result.get("reply_subject")
                    or f"Re: {parsed['subject']}",
                    "body": reply_body,
                    "rationale": rationale,
                    "in_reply_to": parsed["message_id"],
                },
            )
            stats.reply_drafts += 1
        conn.commit()

    return stats


def poll(
    conn: sqlite3.Connection,
    config: Config,
    since_days: int = 7,
    limit: int = 50,
    llm: StructuredLLM | None = None,
) -> InboxStats:
    return process_replies(conn, config, fetch_messages(config, since_days, limit), llm)
