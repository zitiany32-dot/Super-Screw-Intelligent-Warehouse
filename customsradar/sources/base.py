"""海关数据源的统一接口。

不同供应商（腾道、易之家、Panjiva、ImportGenius…）导出的字段五花八门，
所以这里只定义一个标准记录结构，具体适配交给各个 source 实现。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Protocol


@dataclass
class CustomsLead:
    """一条海关提单 + 它归属的目标公司。"""

    company_name: str
    country: str | None = None
    domain: str | None = None
    website: str | None = None
    contact_email: str | None = None
    direction: str | None = "import"
    shipment_date: str | None = None
    hs_code: str | None = None
    product_desc: str | None = None
    quantity: float | None = None
    unit: str | None = None
    value_usd: float | None = None
    supplier: str | None = None
    origin: str | None = None
    destination: str | None = None
    source: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)

    def company_dict(self) -> dict[str, Any]:
        return {
            "name": self.company_name,
            "country": self.country,
            "domain": self.domain,
            "website": self.website,
            "contact_email": self.contact_email,
            "source": self.source,
            "raw": self.raw,
        }

    def record_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "company_name", "country", "domain", "website",
            "contact_email", "source",
        ):
            data.pop(key, None)
        return data


class CustomsSource(Protocol):
    """所有数据源实现这个接口即可接入流水线。"""

    name: str

    def fetch(self) -> Iterable[CustomsLead]:
        ...
