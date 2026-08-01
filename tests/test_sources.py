from __future__ import annotations

from pathlib import Path

import pytest

from boltmind.sources.csv_source import (
    CsvCustomsSource,
    build_mapping,
    extract_domain,
    normalize_date,
    parse_number,
)
from boltmind.sources.demo import DemoCustomsSource


def test_build_mapping_matches_english_and_chinese_headers():
    mapping = build_mapping(
        ["Buyer Name", "Country", "HS Code", "Product Description", "Value (USD)"]
    )
    assert mapping["company_name"] == "Buyer Name"
    assert mapping["hs_code"] == "HS Code"
    assert mapping["value_usd"] == "Value (USD)"

    zh = build_mapping(["采购商", "国家", "HS编码", "产品描述", "金额", "供应商"])
    assert zh["company_name"] == "采购商"
    assert zh["supplier"] == "供应商"


def test_parse_number_handles_dirty_values():
    assert parse_number("1,250") == 1250
    assert parse_number("$3,400.00") == 3400.0
    assert parse_number("480000 PCS") == 480000
    assert parse_number("") is None
    assert parse_number(None) is None
    assert parse_number(42) == 42.0


def test_normalize_date_variants():
    assert normalize_date("2026-01-05") == "2026-01-05"
    assert normalize_date("2026/1/5") == "2026-01-05"
    assert normalize_date("03/15/2026") == "2026-03-15"
    assert normalize_date("") is None


def test_extract_domain():
    assert extract_domain("https://www.acme.example.com/about") == "acme.example.com"
    assert extract_domain("acme.example.com") == "acme.example.com"
    assert extract_domain("not a url") is None
    assert extract_domain(None) is None


def test_csv_source_reads_leads(tmp_path: Path):
    csv_path = tmp_path / "customs.csv"
    csv_path.write_text(
        "Buyer Name,Country,Date,HS Code,Product Description,Quantity,Value USD,Supplier,Website\n"
        "Acme Fasteners Co Ltd,Spain,2026-03-01,731815,HEX BOLTS,\"120,000\",\"$18,000\","
        "Ningbo Co,https://www.acme.example.com\n"
        ",Spain,2026-03-02,731815,HEX NUTS,1000,500,Ningbo Co,\n",
        encoding="utf-8",
    )
    leads = list(CsvCustomsSource(csv_path).fetch())

    assert len(leads) == 1  # 没有公司名的那行被丢掉
    lead = leads[0]
    assert lead.company_name == "Acme Fasteners Co Ltd"
    assert lead.quantity == 120000
    assert lead.value_usd == 18000
    assert lead.domain == "acme.example.com"
    assert lead.website == "https://acme.example.com"
    assert lead.raw["Supplier"] == "Ningbo Co"


def test_csv_source_errors_without_company_column(tmp_path: Path):
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="找不到公司名列"):
        list(CsvCustomsSource(csv_path).fetch())


def test_csv_source_accepts_mapping_override(tmp_path: Path):
    csv_path = tmp_path / "odd.csv"
    csv_path.write_text("客户全称,金额\nAcme SA,1000\n", encoding="utf-8")
    source = CsvCustomsSource(csv_path, mapping={"company_name": "客户全称"})
    leads = list(source.fetch())
    assert leads[0].company_name == "Acme SA"
    assert leads[0].value_usd == 1000


def test_csv_source_reads_a_directory(tmp_path: Path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for i in range(2):
        (inbox / f"f{i}.csv").write_text(
            f"Buyer,Value USD\nCompany {i},100\n", encoding="utf-8"
        )
    assert len(list(CsvCustomsSource(inbox).fetch())) == 2


def test_demo_source_is_clearly_marked_fictional():
    leads = list(DemoCustomsSource(limit=2).fetch())
    assert leads
    assert all(lead.raw.get("demo") is True for lead in leads)
    assert all(
        lead.domain is None or ".example." in lead.domain for lead in leads
    )
