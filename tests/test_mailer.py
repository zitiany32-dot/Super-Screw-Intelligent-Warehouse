"""发送闸门的测试 —— 这一组是整个项目最重要的测试。

「AI 干 80% 的脏活，但扣扳机的永远是你」这句话，如果没有测试守着，
迟早会在某次重构里悄悄失效。
"""

from __future__ import annotations

import pytest

from customsradar import mailer, store


class FakeSMTP:
    sent: list = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def send_message(self, message):
        FakeSMTP.sent.append(message)


@pytest.fixture(autouse=True)
def _clear_sent():
    FakeSMTP.sent = []
    yield
    FakeSMTP.sent = []


@pytest.fixture()
def draft_id(conn):
    company_id = store.upsert_company(conn, {"name": "Acme", "country": "Spain"})
    draft = store.save_draft(
        conn,
        {
            "company_id": company_id,
            "to_email": "buyer@acme.example.com",
            "subject": "Hex bolts",
            "body": "Hello,\n\nWe make grade 8.8 hex bolts.\n\nBest regards",
            "language": "en",
        },
    )
    conn.commit()
    return draft


def test_send_requires_explicit_confirm(conn, config, draft_id):
    config.allow_send = True
    mailer.approve_draft(conn, draft_id)
    with pytest.raises(mailer.SendBlocked, match="confirm=True"):
        mailer.send_draft(conn, config, draft_id, smtp_factory=FakeSMTP)
    assert FakeSMTP.sent == []


def test_send_requires_approved_status(conn, config, draft_id):
    config.allow_send = True
    with pytest.raises(mailer.SendBlocked, match="approved"):
        mailer.send_draft(conn, config, draft_id, confirm=True, smtp_factory=FakeSMTP)
    assert FakeSMTP.sent == []


def test_send_requires_allow_send_flag(conn, config, draft_id):
    config.allow_send = False
    mailer.approve_draft(conn, draft_id)
    with pytest.raises(mailer.SendBlocked, match="ALLOW_SEND"):
        mailer.send_draft(conn, config, draft_id, confirm=True, smtp_factory=FakeSMTP)
    assert FakeSMTP.sent == []


def test_send_blocked_on_unfilled_placeholder(conn, config, draft_id):
    config.allow_send = True
    mailer.approve_draft(
        conn, draft_id, body="Our price is [[待确认: 按规格报价]] per 1000 pcs."
    )
    with pytest.raises(mailer.SendBlocked, match="占位符"):
        mailer.send_draft(conn, config, draft_id, confirm=True, smtp_factory=FakeSMTP)
    assert FakeSMTP.sent == []


def test_send_blocked_without_recipient(conn, config, draft_id):
    config.allow_send = True
    mailer.approve_draft(conn, draft_id, to_email="")
    problems = mailer.preflight(config, store.get_draft(conn, draft_id))
    assert any("收件人" in p for p in problems)


def test_send_blocked_on_malformed_recipient(conn, config, draft_id):
    config.allow_send = True
    mailer.approve_draft(conn, draft_id, to_email="not-an-email")
    with pytest.raises(mailer.SendBlocked, match="格式"):
        mailer.send_draft(conn, config, draft_id, confirm=True, smtp_factory=FakeSMTP)


def test_happy_path_sends_once_and_records_state(conn, config, draft_id):
    config.allow_send = True
    mailer.approve_draft(conn, draft_id)
    result = mailer.send_draft(
        conn, config, draft_id, confirm=True, smtp_factory=FakeSMTP
    )

    assert len(FakeSMTP.sent) == 1
    message = FakeSMTP.sent[0]
    assert message["To"] == "buyer@acme.example.com"
    assert message["Subject"] == "Hex bolts"
    assert "sales@example.com" in message["From"]
    assert message["Message-ID"] == result.message_id
    assert "grade 8.8 hex bolts" in message.get_content()

    draft = store.get_draft(conn, draft_id)
    assert draft["status"] == "sent"
    assert draft["message_id"] == result.message_id
    assert store.get_company(conn, draft["company_id"])["status"] == "contacted"


def test_will_not_send_the_same_draft_twice(conn, config, draft_id):
    config.allow_send = True
    mailer.approve_draft(conn, draft_id)
    mailer.send_draft(conn, config, draft_id, confirm=True, smtp_factory=FakeSMTP)
    with pytest.raises(mailer.SendBlocked, match="已发送"):
        mailer.send_draft(conn, config, draft_id, confirm=True, smtp_factory=FakeSMTP)
    assert len(FakeSMTP.sent) == 1


def test_smtp_failure_marks_draft_failed(conn, config, draft_id):
    class BrokenSMTP(FakeSMTP):
        def send_message(self, message):
            raise OSError("connection refused")

    config.allow_send = True
    mailer.approve_draft(conn, draft_id)
    with pytest.raises(OSError):
        mailer.send_draft(conn, config, draft_id, confirm=True, smtp_factory=BrokenSMTP)

    draft = store.get_draft(conn, draft_id)
    assert draft["status"] == "failed"
    assert "connection refused" in draft["error"]


def test_approve_records_edits(conn, config, draft_id):
    mailer.approve_draft(
        conn, draft_id, subject="Edited subject", body="Edited body",
        to_email="new@acme.example.com",
    )
    draft = store.get_draft(conn, draft_id)
    assert draft["status"] == "approved"
    assert draft["subject"] == "Edited subject"
    assert draft["body"] == "Edited body"
    assert draft["to_email"] == "new@acme.example.com"
    assert draft["reviewed_at"] is not None


def test_reject_draft(conn, config, draft_id):
    mailer.reject_draft(conn, draft_id, "同行，不是客户")
    draft = store.get_draft(conn, draft_id)
    assert draft["status"] == "rejected"
    assert draft["error"] == "同行，不是客户"


def test_reply_draft_carries_threading_headers(conn, config):
    company_id = store.upsert_company(conn, {"name": "Acme"})
    reply_draft = store.save_draft(
        conn,
        {
            "company_id": company_id,
            "kind": "reply",
            "to_email": "buyer@acme.example.com",
            "subject": "Re: Hex bolts",
            "body": "Thanks for getting back to us.",
            "in_reply_to": "<abc123@acme.example.com>",
        },
    )
    conn.commit()
    config.allow_send = True
    mailer.approve_draft(conn, reply_draft)
    mailer.send_draft(conn, config, reply_draft, confirm=True, smtp_factory=FakeSMTP)

    message = FakeSMTP.sent[0]
    assert message["In-Reply-To"] == "<abc123@acme.example.com>"
    assert message["References"] == "<abc123@acme.example.com>"


def test_export_eml_writes_a_file(conn, config, draft_id, tmp_path):
    draft = store.get_draft(conn, draft_id)
    path = mailer.export_eml(config, draft, tmp_path / "eml")
    assert path.exists()
    content = path.read_text(encoding="utf-8", errors="replace")
    assert "buyer@acme.example.com" in content
    assert "Hex bolts" in content


def test_signature_is_appended(conn, config, draft_id):
    config.allow_send = True
    mailer.approve_draft(conn, draft_id)
    mailer.send_draft(conn, config, draft_id, confirm=True, smtp_factory=FakeSMTP)
    body = FakeSMTP.sent[0].get_content()
    assert "Li Wei" in body
    assert config.seller.company_name in body
