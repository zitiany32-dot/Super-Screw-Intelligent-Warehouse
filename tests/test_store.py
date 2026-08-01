from __future__ import annotations

from boltmind import store


def test_normalize_strips_legal_suffixes():
    assert store.normalize_company_name("ACME Fasteners Co., Ltd.") == "acme fasteners"
    assert store.normalize_company_name("Acme Fasteners Limited") == "acme fasteners"
    assert store.normalize_company_name("ACME  FASTENERS") == "acme fasteners"
    assert store.normalize_company_name("Rhein Befestigung GmbH") == "rhein befestigung"


def test_normalize_keeps_something_for_suffix_only_names():
    # 全是后缀时不能返回空串，否则所有这类记录会被合并成同一家公司
    assert store.normalize_company_name("Ltd") == "ltd"


def test_upsert_dedupes_by_normalized_name(conn):
    first = store.upsert_company(
        conn, {"name": "ACME Fasteners Co., Ltd.", "country": "Spain"}
    )
    second = store.upsert_company(
        conn, {"name": "Acme Fasteners Limited", "domain": "acme.example.com"}
    )
    assert first == second

    company = store.get_company(conn, first)
    assert company["country"] == "Spain"          # 首次写入的保留
    assert company["domain"] == "acme.example.com"  # 后来补上的填进去


def test_upsert_does_not_overwrite_existing_fields(conn):
    company_id = store.upsert_company(
        conn, {"name": "Acme", "contact_email": "real@acme.example.com"}
    )
    store.upsert_company(conn, {"name": "Acme", "contact_email": "junk@spam.example"})
    assert store.get_company(conn, company_id)["contact_email"] == "real@acme.example.com"


def test_customs_records_dedupe_on_fingerprint(conn):
    company_id = store.upsert_company(conn, {"name": "Acme"})
    record = {
        "shipment_date": "2026-01-15",
        "hs_code": "731815",
        "product_desc": "HEX BOLTS",
        "quantity": 1000,
        "value_usd": 5000,
        "supplier": "Ningbo Co",
    }
    assert store.add_customs_record(conn, company_id, record) is not None
    assert store.add_customs_record(conn, company_id, record) is None
    assert len(store.get_customs_records(conn, company_id)) == 1


def test_draft_lifecycle(conn):
    company_id = store.upsert_company(conn, {"name": "Acme"})
    draft_id = store.save_draft(
        conn,
        {"company_id": company_id, "subject": "Hi", "body": "Body", "to_email": "a@b.com"},
    )
    assert store.get_draft(conn, draft_id)["status"] == "pending"

    store.update_draft(conn, draft_id, status="approved", subject="Hi there")
    draft = store.get_draft(conn, draft_id)
    assert draft["status"] == "approved"
    assert draft["subject"] == "Hi there"

    # 不在白名单里的字段应该被忽略，防止随手把 company_id 改了
    store.update_draft(conn, draft_id, company_id=999)
    assert store.get_draft(conn, draft_id)["company_id"] == company_id


def test_list_drafts_orders_high_priority_first(conn):
    for name, priority, score in (
        ("Low Co", "low", 20),
        ("High Co", "high", 90),
        ("Mid Co", "medium", 55),
    ):
        company_id = store.upsert_company(conn, {"name": name})
        analysis_id = store.save_analysis(
            conn, company_id, {"priority": priority, "score": score}
        )
        store.save_draft(
            conn, {"company_id": company_id, "analysis_id": analysis_id, "subject": name}
        )
    names = [d["company_name"] for d in store.list_drafts(conn)]
    assert names == ["High Co", "Mid Co", "Low Co"]


def test_run_stats_roundtrip(conn):
    run_id = store.start_run(conn)
    store.finish_run(conn, run_id, {"analyzed": 3}, 0.42, "ok")
    run = store.get_run(conn, run_id)
    assert run["stats"]["analyzed"] == 3
    assert run["cost_usd"] == 0.42
    assert store.latest_run(conn)["id"] == run_id
