"""演示数据源：虚构的紧固件进口商，用来跑通流水线。

⚠️ 里面所有公司、域名、提单都是编的（域名统一用 IANA 保留的 example.* 段），
不要当成真实线索。买到真实海关数据后换成 CsvCustomsSource 即可。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterator

from .base import CustomsLead

_DEMO_ROWS: list[dict] = [
    {
        "company_name": "Northwind Fasteners BV",
        "country": "Netherlands",
        "domain": "northwind-fasteners.example.com",
        "hs_code": "731815",
        "product_desc": "HEX BOLTS GRADE 8.8 ZINC PLATED M6-M12",
        "quantity": 480000,
        "unit": "PCS",
        "value_usd": 62000,
        "supplier": "Ningbo Yifeng Hardware Co Ltd",
        "origin": "China",
        "shipments": 6,
    },
    {
        "company_name": "Andes Industrial Supply SA",
        "country": "Chile",
        "domain": "andes-industrial.example.com",
        "hs_code": "731816",
        "product_desc": "HEX NUTS DIN934 CLASS 8 ZINC",
        "quantity": 1200000,
        "unit": "PCS",
        "value_usd": 41000,
        "supplier": "Handan Yongnian Fastener Factory",
        "origin": "China",
        "shipments": 4,
    },
    {
        "company_name": "Baltic Bolt & Nut OY",
        "country": "Finland",
        "domain": "balticbolt.example.com",
        "hs_code": "731815",
        "product_desc": "FLANGE NUTS M8 STAINLESS A2",
        "quantity": 260000,
        "unit": "PCS",
        "value_usd": 28500,
        "supplier": "Taiwan Precision Fastener Corp",
        "origin": "Taiwan",
        "shipments": 3,
    },
    {
        "company_name": "Sahara Construction Materials LLC",
        "country": "United Arab Emirates",
        "domain": None,
        "hs_code": "731822",
        "product_desc": "FLAT WASHERS + SPRING WASHERS ASSORTED",
        "quantity": 3000000,
        "unit": "PCS",
        "value_usd": 19000,
        "supplier": "Hebei Jinshi Metal Products",
        "origin": "China",
        "shipments": 2,
    },
    {
        "company_name": "Great Lakes Assembly Corp",
        "country": "United States",
        "domain": "greatlakesassembly.example.net",
        "hs_code": "731815",
        "product_desc": "SOCKET HEAD CAP SCREWS ALLOY STEEL BLACK OXIDE",
        "quantity": 150000,
        "unit": "PCS",
        "value_usd": 88000,
        "supplier": "Suzhou Hanwei Fastener Co Ltd",
        "origin": "China",
        "shipments": 8,
    },
    {
        "company_name": "Pampa Agro Equipment SRL",
        "country": "Argentina",
        "domain": "pampaagro.example.com",
        "hs_code": "731816",
        "product_desc": "NYLON INSERT LOCK NUTS DIN985 M10",
        "quantity": 340000,
        "unit": "PCS",
        "value_usd": 22000,
        "supplier": "Zhejiang Sunrise Hardware",
        "origin": "China",
        "shipments": 3,
    },
    {
        "company_name": "Rhein Befestigungstechnik GmbH",
        "country": "Germany",
        "domain": "rhein-befestigung.example.com",
        "hs_code": "731815",
        "product_desc": "STUD BOLTS B7 WITH 2H NUTS",
        "quantity": 90000,
        "unit": "PCS",
        "value_usd": 115000,
        "supplier": "Jiangsu Boda Fastener",
        "origin": "China",
        "shipments": 5,
    },
    {
        "company_name": "Mekong Hardware Trading JSC",
        "country": "Vietnam",
        "domain": "mekonghardware.example.com",
        "hs_code": "731816",
        "product_desc": "HEX NUTS M4-M10 CARBON STEEL",
        "quantity": 800000,
        "unit": "PCS",
        "value_usd": 24000,
        "supplier": "Handan Yongnian Fastener Factory",
        "origin": "China",
        "shipments": 5,
    },
]


class DemoCustomsSource:
    """生成一批虚构线索，供本地演示 / 冒烟测试。"""

    name = "demo"

    def __init__(self, limit: int | None = None, today: date | None = None) -> None:
        self.limit = limit
        self.today = today or date.today()

    def fetch(self) -> Iterator[CustomsLead]:
        rows = _DEMO_ROWS[: self.limit] if self.limit else _DEMO_ROWS
        for index, row in enumerate(rows):
            shipments = int(row.get("shipments", 1))
            for n in range(shipments):
                # 把多票提单摊在最近几个月里，好让打分能看出「活跃度」
                shipment_day = self.today - timedelta(days=14 * n + index)
                yield CustomsLead(
                    company_name=row["company_name"],
                    country=row.get("country"),
                    domain=row.get("domain"),
                    website=(
                        f"https://{row['domain']}" if row.get("domain") else None
                    ),
                    direction="import",
                    shipment_date=shipment_day.isoformat(),
                    hs_code=row.get("hs_code"),
                    product_desc=row.get("product_desc"),
                    quantity=row.get("quantity"),
                    unit=row.get("unit"),
                    value_usd=row.get("value_usd"),
                    supplier=row.get("supplier"),
                    origin=row.get("origin"),
                    destination=row.get("country"),
                    source=self.name,
                    raw={"demo": True, "note": "虚构数据，仅供演示"},
                )
