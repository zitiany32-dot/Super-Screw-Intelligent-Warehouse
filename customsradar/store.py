"""数据访问层：所有 SQL 集中在这里，上层逻辑不写裸 SQL。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

from .db import dumps, loads, row_to_dict, rows_to_dicts, utcnow

_LEGAL_SUFFIXES = (
    "co ltd", "co. ltd", "company limited", "limited", "ltd", "llc", "inc",
    "incorporated", "corp", "corporation", "gmbh", "srl", "spa", "sa", "nv",
    "bv", "ab", "as", "oy", "plc", "pty", "sarl", "sas", "kft", "sp z oo",
    "s r o", "pvt", "private limited", "sdn bhd", "bhd",
)


def normalize_company_name(name: str) -> str:
    """归一化公司名，作为去重键。

    海关数据里同一家公司经常写成 "ACME FASTENERS CO., LTD." / "Acme Fasteners Ltd"，
    去掉标点、法律后缀、多余空格后就能合并。
    """
    key = (name or "").lower().strip()
    key = re.sub(r"[^\w\s]", " ", key, flags=re.UNICODE)
    key = re.sub(r"\s+", " ", key).strip()
    changed = True
    while changed and key:
        changed = False
        for suffix in sorted(_LEGAL_SUFFIXES, key=len, reverse=True):
            if key.endswith(" " + suffix):
                key = key[: -(len(suffix) + 1)].strip()
                changed = True
                break
    return key or (name or "").lower().strip()


def fingerprint(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p).strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# companies
# --------------------------------------------------------------------------- #

def find_company_id(conn: sqlite3.Connection, name: str) -> int | None:
    """按归一化名查公司 id。走 name_key 唯一索引，比 COUNT(*) 便宜得多。"""
    row = conn.execute(
        "SELECT id FROM companies WHERE name_key = ?",
        (normalize_company_name(name),),
    ).fetchone()
    return int(row["id"]) if row else None


def upsert_company(conn: sqlite3.Connection, company: dict[str, Any]) -> int:
    """按归一化名去重写入公司，返回 company_id。已存在则补齐空字段。"""
    name = (company.get("name") or "").strip()
    if not name:
        raise ValueError("company name is required")
    key = normalize_company_name(name)
    now = utcnow()

    existing = conn.execute(
        "SELECT * FROM companies WHERE name_key = ?", (key,)
    ).fetchone()

    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO companies
                (name, name_key, country, domain, website, contact_email,
                 source, status, first_seen, last_seen, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
            """,
            (
                name,
                key,
                company.get("country"),
                company.get("domain"),
                company.get("website"),
                company.get("contact_email"),
                company.get("source"),
                now,
                now,
                dumps(company.get("raw") or {}),
            ),
        )
        return int(cur.lastrowid)

    # 已存在：只填补空字段，不覆盖已有信息（人工填的域名不该被脏数据冲掉）
    updates: dict[str, Any] = {"last_seen": now}
    for field in ("country", "domain", "website", "contact_email"):
        if not existing[field] and company.get(field):
            updates[field] = company[field]
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE companies SET {assignments} WHERE id = ?",
        (*updates.values(), existing["id"]),
    )
    return int(existing["id"])


def set_company_status(conn: sqlite3.Connection, company_id: int, status: str) -> None:
    conn.execute(
        "UPDATE companies SET status = ?, last_seen = ? WHERE id = ?",
        (status, utcnow(), company_id),
    )


def get_company(conn: sqlite3.Connection, company_id: int) -> dict[str, Any] | None:
    return row_to_dict(
        conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    )


def list_companies(
    conn: sqlite3.Connection, status: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    if status:
        rows = conn.execute(
            "SELECT * FROM companies WHERE status = ? ORDER BY last_seen DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM companies ORDER BY last_seen DESC LIMIT ?", (limit,)
        ).fetchall()
    return rows_to_dicts(rows)


# --------------------------------------------------------------------------- #
# customs records
# --------------------------------------------------------------------------- #

def add_customs_record(
    conn: sqlite3.Connection, company_id: int, record: dict[str, Any]
) -> int | None:
    """写入一条提单。重复记录（同指纹）直接跳过，返回 None。"""
    fp = record.get("fingerprint") or fingerprint(
        company_id,
        record.get("shipment_date"),
        record.get("hs_code"),
        record.get("product_desc"),
        record.get("quantity"),
        record.get("value_usd"),
        record.get("supplier"),
    )
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO customs_records
            (company_id, fingerprint, direction, shipment_date, hs_code,
             product_desc, quantity, unit, value_usd, supplier, origin,
             destination, raw, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            fp,
            record.get("direction"),
            record.get("shipment_date"),
            record.get("hs_code"),
            record.get("product_desc"),
            record.get("quantity"),
            record.get("unit"),
            record.get("value_usd"),
            record.get("supplier"),
            record.get("origin"),
            record.get("destination"),
            dumps(record.get("raw") or {}),
            utcnow(),
        ),
    )
    return int(cur.lastrowid) if cur.rowcount else None


def get_customs_records(
    conn: sqlite3.Connection, company_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM customs_records WHERE company_id = ?
        ORDER BY COALESCE(shipment_date, '') DESC LIMIT ?
        """,
        (company_id, limit),
    ).fetchall()
    return rows_to_dicts(rows)


# --------------------------------------------------------------------------- #
# enrichment
# --------------------------------------------------------------------------- #

def save_enrichment(
    conn: sqlite3.Connection, company_id: int, data: dict[str, Any]
) -> None:
    conn.execute(
        """
        INSERT INTO enrichment
            (company_id, fetched_at, homepage_url, pages, text, emails, phones, status, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
            fetched_at=excluded.fetched_at, homepage_url=excluded.homepage_url,
            pages=excluded.pages, text=excluded.text, emails=excluded.emails,
            phones=excluded.phones, status=excluded.status, error=excluded.error
        """,
        (
            company_id,
            utcnow(),
            data.get("homepage_url"),
            dumps(data.get("pages") or []),
            data.get("text"),
            dumps(data.get("emails") or []),
            dumps(data.get("phones") or []),
            data.get("status", "ok"),
            data.get("error"),
        ),
    )


def get_enrichment(conn: sqlite3.Connection, company_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM enrichment WHERE company_id = ?", (company_id,)
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["pages"] = loads(data.get("pages"), [])
    data["emails"] = loads(data.get("emails"), [])
    data["phones"] = loads(data.get("phones"), [])
    return data


# --------------------------------------------------------------------------- #
# email candidates
# --------------------------------------------------------------------------- #

def save_email_candidates(
    conn: sqlite3.Connection, company_id: int, candidates: list[dict[str, Any]]
) -> None:
    for cand in candidates:
        conn.execute(
            """
            INSERT INTO email_candidates
                (company_id, email, source, sources, confidence, role, verified,
                 person_name, person_title, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, email) DO UPDATE SET
                source=excluded.source, sources=excluded.sources,
                confidence=excluded.confidence, role=excluded.role,
                verified=excluded.verified, person_name=excluded.person_name,
                person_title=excluded.person_title, note=excluded.note
            """,
            (
                company_id,
                cand["email"],
                cand.get("source"),
                dumps(cand.get("sources") or []),
                cand.get("confidence", 0),
                cand.get("role"),
                cand.get("verified"),
                cand.get("person_name"),
                cand.get("person_title"),
                cand.get("note"),
                utcnow(),
            ),
        )


def get_email_candidates(
    conn: sqlite3.Connection, company_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM email_candidates WHERE company_id = ?
        ORDER BY confidence DESC, id ASC
        """,
        (company_id,),
    ).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["sources"] = loads(data.get("sources"), [])
        result.append(data)
    return result


# --------------------------------------------------------------------------- #
# analyses
# --------------------------------------------------------------------------- #

def save_analysis(
    conn: sqlite3.Connection, company_id: int, analysis: dict[str, Any]
) -> int:
    cur = conn.execute(
        """
        INSERT INTO analyses
            (company_id, run_id, created_at, model, priority, score, prescore,
             profile, assessment, contacts, angles, reasons, risks,
             input_tokens, output_tokens, cost_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            analysis.get("run_id"),
            utcnow(),
            analysis.get("model"),
            analysis.get("priority"),
            analysis.get("score"),
            analysis.get("prescore"),
            dumps(analysis.get("profile") or {}),
            dumps(analysis.get("assessment") or {}),
            dumps(analysis.get("contacts") or []),
            dumps(analysis.get("angles") or []),
            analysis.get("reasons"),
            analysis.get("risks"),
            analysis.get("input_tokens", 0),
            analysis.get("output_tokens", 0),
            analysis.get("cost_usd", 0.0),
        ),
    )
    return int(cur.lastrowid)


def get_analysis(conn: sqlite3.Connection, analysis_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM analyses WHERE id = ?", (analysis_id,)
    ).fetchone()
    return _hydrate_analysis(row)


def latest_analysis(
    conn: sqlite3.Connection, company_id: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM analyses WHERE company_id = ? ORDER BY id DESC LIMIT 1",
        (company_id,),
    ).fetchone()
    return _hydrate_analysis(row)


def _hydrate_analysis(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["profile"] = loads(data.get("profile"), {})
    data["assessment"] = loads(data.get("assessment"), {})
    data["contacts"] = loads(data.get("contacts"), [])
    data["angles"] = loads(data.get("angles"), [])
    return data


# --------------------------------------------------------------------------- #
# drafts
# --------------------------------------------------------------------------- #

def save_draft(conn: sqlite3.Connection, draft: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO drafts
            (company_id, analysis_id, reply_id, run_id, kind, created_at, to_email,
             language, subject, body, rationale, status, in_reply_to)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            draft["company_id"],
            draft.get("analysis_id"),
            draft.get("reply_id"),
            draft.get("run_id"),
            draft.get("kind", "cold"),
            utcnow(),
            draft.get("to_email"),
            draft.get("language"),
            draft.get("subject"),
            draft.get("body"),
            draft.get("rationale"),
            draft.get("in_reply_to"),
        ),
    )
    return int(cur.lastrowid)


def get_draft(conn: sqlite3.Connection, draft_id: int) -> dict[str, Any] | None:
    return row_to_dict(
        conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    )


def update_draft(conn: sqlite3.Connection, draft_id: int, **fields: Any) -> None:
    allowed = {
        "to_email", "subject", "body", "language", "status", "reviewed_at",
        "sent_at", "message_id", "in_reply_to", "error", "rationale",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE drafts SET {assignments} WHERE id = ?",
        (*updates.values(), draft_id),
    )


def list_drafts(
    conn: sqlite3.Connection,
    status: str | None = "pending",
    kind: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses, params = [], []
    if status:
        clauses.append("d.status = ?")
        params.append(status)
    if kind:
        clauses.append("d.kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT d.*, c.name AS company_name, c.country AS company_country,
               a.priority AS priority, a.score AS score, a.reasons AS reasons
        FROM drafts d
        LEFT JOIN companies c ON c.id = d.company_id
        LEFT JOIN analyses a ON a.id = d.analysis_id
        {where}
        ORDER BY CASE a.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                 a.score DESC, d.id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return rows_to_dicts(rows)


def find_draft_by_message_id(
    conn: sqlite3.Connection, message_id: str
) -> dict[str, Any] | None:
    return row_to_dict(
        conn.execute(
            "SELECT * FROM drafts WHERE message_id = ?", (message_id,)
        ).fetchone()
    )


# --------------------------------------------------------------------------- #
# replies
# --------------------------------------------------------------------------- #

def save_reply(conn: sqlite3.Connection, reply: dict[str, Any]) -> int | None:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO replies
            (company_id, draft_id, received_at, from_email, subject, body,
             message_id, in_reply_to, intent, urgency, summary, analysis, handled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            reply.get("company_id"),
            reply.get("draft_id"),
            reply.get("received_at") or utcnow(),
            reply.get("from_email"),
            reply.get("subject"),
            reply.get("body"),
            reply.get("message_id"),
            reply.get("in_reply_to"),
            reply.get("intent"),
            reply.get("urgency"),
            reply.get("summary"),
            dumps(reply.get("analysis") or {}),
        ),
    )
    return int(cur.lastrowid) if cur.rowcount else None


def update_reply(conn: sqlite3.Connection, reply_id: int, **fields: Any) -> None:
    allowed = {"intent", "urgency", "summary", "analysis", "handled", "company_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "analysis" in updates and not isinstance(updates["analysis"], str):
        updates["analysis"] = dumps(updates["analysis"])
    if not updates:
        return
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE replies SET {assignments} WHERE id = ?",
        (*updates.values(), reply_id),
    )


def get_reply(conn: sqlite3.Connection, reply_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM replies WHERE id = ?", (reply_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["analysis"] = loads(data.get("analysis"), {})
    return data


def list_replies(
    conn: sqlite3.Connection, handled: bool | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    if handled is None:
        rows = conn.execute(
            """
            SELECT r.*, c.name AS company_name FROM replies r
            LEFT JOIN companies c ON c.id = r.company_id
            ORDER BY r.received_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT r.*, c.name AS company_name FROM replies r
            LEFT JOIN companies c ON c.id = r.company_id
            WHERE r.handled = ? ORDER BY r.received_at DESC LIMIT ?
            """,
            (1 if handled else 0, limit),
        ).fetchall()
    return rows_to_dicts(rows)


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #

def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, status) VALUES (?, 'running')", (utcnow(),)
    )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    stats: dict[str, Any],
    cost_usd: float,
    status: str = "ok",
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = ?, status = ?, stats = ?, cost_usd = ?, error = ?
        WHERE id = ?
        """,
        (utcnow(), status, dumps(stats), cost_usd, error, run_id),
    )


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["stats"] = loads(data.get("stats"), {})
    return data


def latest_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["stats"] = loads(data.get("stats"), {})
    return data


def companies_for_run(
    conn: sqlite3.Connection, run_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.*, c.name AS company_name, c.country AS company_country,
               c.website AS website, c.id AS company_id
        FROM analyses a JOIN companies c ON c.id = a.company_id
        WHERE a.run_id = ?
        ORDER BY CASE a.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                 a.score DESC
        """,
        (run_id,),
    ).fetchall()
    return [_hydrate_analysis(r) for r in rows]  # type: ignore[misc]
