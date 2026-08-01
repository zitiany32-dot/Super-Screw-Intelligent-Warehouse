from __future__ import annotations

from datetime import date, timedelta

from boltmind.scoring import prescore, priority_from_score

TODAY = date(2026, 8, 1)


def _record(days_ago: int, **overrides):
    record = {
        "shipment_date": (TODAY - timedelta(days=days_ago)).isoformat(),
        "hs_code": "731815",
        "product_desc": "HEX BOLTS GRADE 8.8",
        "quantity": 100000,
        "value_usd": 30000,
        "supplier": "Ningbo Hardware Co",
        "origin": "China",
    }
    record.update(overrides)
    return record


def test_active_relevant_buyer_scores_high():
    company = {
        "country": "Germany",
        "website": "https://x.example.com",
        "contact_email": "a@x.example.com",
    }
    records = [_record(10), _record(45), _record(80), _record(120)]
    score, reasons = prescore(company, records, today=TODAY)
    assert score >= 70
    assert priority_from_score(score) == "high"
    assert any("紧固件" in r for r in reasons)


def test_stale_lead_scores_lower_than_fresh_one():
    company = {"country": "Germany", "website": "https://x.example.com"}
    fresh, _ = prescore(company, [_record(10)], today=TODAY)
    stale, _ = prescore(company, [_record(700)], today=TODAY)
    assert fresh > stale


def test_irrelevant_category_scores_low():
    company = {"country": "Germany"}
    records = [
        _record(20, hs_code="620342", product_desc="COTTON TROUSERS", origin="Bangladesh")
    ]
    score, _ = prescore(company, records, today=TODAY)
    assert priority_from_score(score) == "low"


def test_missing_website_is_called_out():
    score, reasons = prescore({"country": "Chile"}, [_record(30)], today=TODAY)
    assert any("没有官网" in r for r in reasons)


def test_score_is_bounded():
    company = {
        "country": "United States",
        "website": "https://x.example.com",
        "contact_email": "a@x.example.com",
    }
    records = [_record(i, value_usd=500000) for i in range(1, 20)]
    score, _ = prescore(company, records, today=TODAY)
    assert 0 <= score <= 100


def test_no_records_still_returns_a_score():
    score, reasons = prescore({"country": "Peru"}, [], today=TODAY)
    assert 0 <= score <= 100
    assert isinstance(reasons, list)


def test_priority_thresholds():
    assert priority_from_score(90) == "high"
    assert priority_from_score(70) == "high"
    assert priority_from_score(50) == "medium"
    assert priority_from_score(10) == "low"
