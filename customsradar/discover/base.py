"""邮箱发现的统一数据结构和接口。

「不管去什么软件、什么方法，只要能合法拿到客户邮箱」—— 所以设计成多源聚合：
每个来源是一个 provider，实现 find() 返回候选邮箱，聚合器负责合并、去重、
打分、校验。加一个新来源（比如某个付费数据库的 API）只要写一个 provider。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}$")

# 业务联系人优先级：sales/询价类高，hr/法务/隐私类低
_ROLE_HIGH = (
    "sales", "info", "contact", "enquiry", "inquiry", "commercial", "purchas",
    "procurement", "office", "hello", "export", "import", "trade", "ventas",
    "vertrieb", "compras", "achat", "acquisti",
)
_ROLE_LOW = (
    "hr", "jobs", "career", "recruit", "legal", "privacy", "abuse", "press",
    "webmaster", "postmaster", "noreply", "no-reply", "donotreply", "mailer",
)


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match((email or "").strip()))


def classify_role(email: str) -> str:
    """粗分邮箱类型：business / personal / generic / low。"""
    local = (email or "").split("@", 1)[0].lower()
    if any(word in local for word in _ROLE_LOW):
        return "low"
    if any(word in local for word in _ROLE_HIGH):
        return "business"
    # 带点或看起来像人名的，多半是个人邮箱（决策人）
    if "." in local or (local.isalpha() and 3 <= len(local) <= 20):
        return "personal"
    return "generic"


@dataclass
class EmailCandidate:
    """一个候选邮箱 + 它从哪来、多可信、验没验过。"""

    email: str
    source: str                       # website / whois / search / hunter / pattern / ...
    confidence: int = 50              # 0-100
    role: str = "generic"
    verified: str = "syntax"          # syntax / mx / domain_resolves / smtp_ok / smtp_unknown / smtp_fail
    person_name: str | None = None    # 如果对应到具体的人
    person_title: str | None = None
    note: str | None = None
    sources: list[str] = field(default_factory=list)  # 多个来源都命中时合并

    def __post_init__(self) -> None:
        self.email = (self.email or "").strip().lower()
        if not self.role or self.role == "generic":
            self.role = classify_role(self.email)
        if not self.sources:
            self.sources = [self.source]

    @property
    def valid(self) -> bool:
        return is_valid_email(self.email)

    def as_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "source": self.source,
            "sources": self.sources,
            "confidence": self.confidence,
            "role": self.role,
            "verified": self.verified,
            "person_name": self.person_name,
            "person_title": self.person_title,
            "note": self.note,
        }


@dataclass
class DiscoveryContext:
    """喂给各个 provider 的输入。"""

    company_name: str
    domain: str | None = None
    website: str | None = None
    country: str | None = None
    known_emails: list[str] = field(default_factory=list)
    site_text: str | None = None
    # 从官网/分析里抽到的真实人名（用于生成邮箱模式），绝不含编造的
    people: list[dict[str, Any]] = field(default_factory=list)


class EmailProvider(Protocol):
    name: str

    def find(self, context: DiscoveryContext) -> Iterable[EmailCandidate]:
        ...
