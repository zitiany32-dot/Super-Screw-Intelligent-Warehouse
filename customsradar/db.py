"""SQLite 存储层。只用标准库，没有 ORM，方便直接 sqlite3 命令行查数据。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 海关数据线索归拢出的目标公司
CREATE TABLE IF NOT EXISTS companies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    name_key     TEXT NOT NULL UNIQUE,   -- 归一化去重键
    country      TEXT,
    domain       TEXT,
    website      TEXT,
    contact_email TEXT,
    source       TEXT,
    status       TEXT NOT NULL DEFAULT 'new',  -- new/enriched/analyzed/drafted/contacted/replied/skipped
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_companies_status ON companies(status);

-- 单条海关提单记录
CREATE TABLE IF NOT EXISTS customs_records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    fingerprint  TEXT NOT NULL UNIQUE,
    direction    TEXT,          -- import / export
    shipment_date TEXT,
    hs_code      TEXT,
    product_desc TEXT,
    quantity     REAL,
    unit         TEXT,
    value_usd    REAL,
    supplier     TEXT,
    origin       TEXT,
    destination  TEXT,
    raw          TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customs_company ON customs_records(company_id);
CREATE INDEX IF NOT EXISTS idx_customs_date ON customs_records(shipment_date);

-- 官网抓取结果
CREATE TABLE IF NOT EXISTS enrichment (
    company_id   INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    fetched_at   TEXT NOT NULL,
    homepage_url TEXT,
    pages        TEXT,          -- JSON: [{url, title, chars}]
    text         TEXT,
    emails       TEXT,          -- JSON list
    phones       TEXT,          -- JSON list
    status       TEXT NOT NULL, -- ok / no_site / blocked / error
    error        TEXT
);

-- 多源发现的候选邮箱（官网/WHOIS/搜索/Hunter/人名模式）
CREATE TABLE IF NOT EXISTS email_candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    email        TEXT NOT NULL,
    source       TEXT,
    sources      TEXT,          -- JSON list
    confidence   INTEGER DEFAULT 0,
    role         TEXT,
    verified     TEXT,
    person_name  TEXT,
    person_title TEXT,
    note         TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE(company_id, email)
);
CREATE INDEX IF NOT EXISTS idx_email_candidates_company ON email_candidates(company_id);

-- AI 生成的公司画像 + 切入点分析
CREATE TABLE IF NOT EXISTS analyses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    run_id       INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL,
    model        TEXT,
    priority     TEXT,          -- high / medium / low
    score        INTEGER,
    prescore     INTEGER,
    profile      TEXT,          -- JSON
    assessment   TEXT,          -- JSON: 利弊评估 strengths/weaknesses/opportunities/threats
    contacts     TEXT,          -- JSON: 抓到的真实联系人
    angles       TEXT,          -- JSON
    reasons      TEXT,
    risks        TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd     REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_analyses_company ON analyses(company_id);

-- 开发信 / 回信草稿。人工点发送前一律停在这里。
CREATE TABLE IF NOT EXISTS drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    analysis_id  INTEGER REFERENCES analyses(id) ON DELETE SET NULL,
    reply_id     INTEGER REFERENCES replies(id) ON DELETE SET NULL,
    run_id       INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL DEFAULT 'cold',   -- cold / reply
    created_at   TEXT NOT NULL,
    to_email     TEXT,
    language     TEXT,
    subject      TEXT,
    body         TEXT,
    rationale    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending', -- pending/approved/rejected/sent/failed
    reviewed_at  TEXT,
    sent_at      TEXT,
    message_id   TEXT,
    in_reply_to  TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_company ON drafts(company_id);

-- 收到的回复 + AI 意图分析
CREATE TABLE IF NOT EXISTS replies (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id   INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    draft_id     INTEGER REFERENCES drafts(id) ON DELETE SET NULL,
    received_at  TEXT NOT NULL,
    from_email   TEXT,
    subject      TEXT,
    body         TEXT,
    message_id   TEXT UNIQUE,
    in_reply_to  TEXT,
    intent       TEXT,
    urgency      TEXT,
    summary      TEXT,
    analysis     TEXT,          -- JSON
    handled      INTEGER NOT NULL DEFAULT 0
);

-- 每晚一次的跑批记录
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    stats        TEXT,
    cost_usd     REAL DEFAULT 0,
    error        TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path | str) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def session(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = init_db(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
