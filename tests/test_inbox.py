from __future__ import annotations

from conftest import FakeLLM

from customsradar import inbox, store

RAW_REPLY = b"""\
From: Buyer Name <buyer@acme.example.com>
To: sales@example.com
Subject: Re: Hex bolts
Date: Mon, 3 Aug 2026 09:15:00 +0200
Message-ID: <reply-1@acme.example.com>
In-Reply-To: <sent-1@example.com>
Content-Type: text/plain; charset="utf-8"

Hello,

Please send pricing for DIN 933 M8x40 zinc, 50,000 pcs.

On Sun, 2 Aug 2026, sales@example.com wrote:
> Hello, I saw that you import grade 8.8 hex bolts.
> Best regards
"""

RAW_AUTOREPLY = b"""\
From: Buyer Name <buyer@acme.example.com>
Subject: Out of office
Message-ID: <auto-1@acme.example.com>
In-Reply-To: <sent-1@example.com>
Auto-Submitted: auto-replied
Content-Type: text/plain; charset="utf-8"

I am away until September.
"""

RAW_HTML_ONLY = b"""\
From: someone@acme.example.com
Subject: Re: Hex bolts
Message-ID: <html-1@acme.example.com>
In-Reply-To: <sent-1@example.com>
Content-Type: text/html; charset="utf-8"

<html><body><p>Send me a <b>quotation</b> please.</p></body></html>
"""

FAKE_REPLY_ANALYSIS = {
    "intent": "asking_price",
    "urgency": "high",
    "summary": "对方要 DIN 933 M8x40 镀锌 5 万支的报价。",
    "key_points": ["DIN 933 M8x40 镀锌", "50,000 pcs"],
    "objections": "无",
    "recommended_action": "按规格核算价格后回复",
    "needs_human_input": "单价、交期",
    "reply_subject": "Re: Hex bolts",
    "reply_body": "Thanks for the details. Our price is [[待确认: 单价]].",
    "reply_language": "en",
    "rationale": "先确认规格再报价。",
}


def _seed_sent_draft(conn):
    company_id = store.upsert_company(
        conn,
        {
            "name": "Acme Fasteners",
            "domain": "acme.example.com",
            "contact_email": "buyer@acme.example.com",
        },
    )
    draft_id = store.save_draft(
        conn,
        {
            "company_id": company_id,
            "to_email": "buyer@acme.example.com",
            "subject": "Hex bolts",
            "body": "Hello",
        },
    )
    store.update_draft(
        conn, draft_id, status="sent", message_id="<sent-1@example.com>"
    )
    conn.commit()
    return company_id, draft_id


def test_parse_message_extracts_headers_and_strips_quote():
    parsed = inbox.parse_message(RAW_REPLY)
    assert parsed["from_email"] == "buyer@acme.example.com"
    assert parsed["message_id"] == "<reply-1@acme.example.com>"
    assert parsed["in_reply_to"] == "<sent-1@example.com>"
    assert "DIN 933 M8x40" in parsed["body"]
    assert "I saw that you import" not in parsed["body"]  # 引用原文被剪掉
    assert parsed["auto_reply"] is False


def test_parse_message_detects_autoreply():
    assert inbox.parse_message(RAW_AUTOREPLY)["auto_reply"] is True


def test_parse_message_falls_back_to_html():
    parsed = inbox.parse_message(RAW_HTML_ONLY)
    assert "quotation" in parsed["body"]
    assert "<b>" not in parsed["body"]


def test_strip_quoted_handles_chinese_quote_marker():
    body = "好的，请报价。\n\n在 2026年8月2日，sales@example.com 写道：\n> 原文"
    assert "原文" not in inbox.strip_quoted(body)
    assert "请报价" in inbox.strip_quoted(body)


def test_strip_quoted_keeps_body_when_nothing_matches():
    assert inbox.strip_quoted("just a plain reply") == "just a plain reply"


def test_match_thread_by_message_id(conn):
    company_id, draft_id = _seed_sent_draft(conn)
    draft, company = inbox.match_thread(conn, inbox.parse_message(RAW_REPLY))
    assert draft["id"] == draft_id
    assert company["id"] == company_id


def test_match_thread_by_sender_when_headers_missing(conn):
    company_id, _ = _seed_sent_draft(conn)
    parsed = {"in_reply_to": "", "references": [], "from_email": "buyer@acme.example.com"}
    _, company = inbox.match_thread(conn, parsed)
    assert company["id"] == company_id


def test_match_thread_returns_none_for_strangers(conn):
    _seed_sent_draft(conn)
    parsed = {"in_reply_to": "", "references": [], "from_email": "nobody@elsewhere.example"}
    draft, company = inbox.match_thread(conn, parsed)
    assert draft is None and company is None


def test_process_replies_creates_reply_draft_not_a_send(conn, config):
    _seed_sent_draft(conn)
    llm = FakeLLM({"reply:": FAKE_REPLY_ANALYSIS})
    stats = inbox.process_replies(conn, config, [RAW_REPLY], llm=llm)

    assert stats.new_replies == 1
    assert stats.reply_drafts == 1

    reply = store.list_replies(conn)[0]
    assert reply["intent"] == "asking_price"
    assert "报价" in reply["summary"]

    drafts = store.list_drafts(conn, status="pending", kind="reply")
    assert len(drafts) == 1
    assert drafts[0]["in_reply_to"] == "<reply-1@acme.example.com>"
    assert drafts[0]["status"] == "pending"
    # 需要人工填的地方保留了占位符，mailer 的闸门会拦下未填就发送
    assert "[[待确认" in drafts[0]["body"]


def test_process_replies_is_idempotent(conn, config):
    _seed_sent_draft(conn)
    llm = FakeLLM({"reply:": FAKE_REPLY_ANALYSIS})
    inbox.process_replies(conn, config, [RAW_REPLY], llm=llm)
    second = inbox.process_replies(conn, config, [RAW_REPLY], llm=llm)
    assert second.new_replies == 0
    assert len(store.list_drafts(conn, status=None, kind="reply")) == 1


def test_autoreply_is_recorded_but_not_sent_to_the_model(conn, config):
    _seed_sent_draft(conn)
    llm = FakeLLM({"reply:": FAKE_REPLY_ANALYSIS})
    stats = inbox.process_replies(conn, config, [RAW_AUTOREPLY], llm=llm)
    assert stats.new_replies == 1
    assert stats.analyzed == 0
    assert llm.calls == []  # 自动回复不该烧 AI 预算
    assert store.list_replies(conn)[0]["intent"] == "out_of_office"


def test_unsubscribe_intent_produces_no_reply_draft(conn, config):
    _seed_sent_draft(conn)
    llm = FakeLLM(
        {
            "reply:": {
                **FAKE_REPLY_ANALYSIS,
                "intent": "unsubscribe",
                "reply_body": "Understood, removing you from our list.",
                "recommended_action": "加入不再联系名单",
            }
        }
    )
    stats = inbox.process_replies(conn, config, [RAW_REPLY], llm=llm)
    assert stats.new_replies == 1
    assert stats.reply_drafts == 0


def test_unmatched_message_costs_nothing(conn, config):
    llm = FakeLLM({"reply:": FAKE_REPLY_ANALYSIS})
    stats = inbox.process_replies(conn, config, [RAW_REPLY], llm=llm)
    assert stats.unmatched == 1
    assert stats.new_replies == 0
    assert llm.calls == []
