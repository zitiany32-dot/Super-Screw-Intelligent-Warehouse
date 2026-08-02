"""按「真实人名 + 域名」生成候选邮箱模式，再由校验环节确认真假。

这是行业标准做法（Hunter、Apollo 内部都这么干）：不是瞎猜，而是穷举常见的
命名规则（first.last@、flast@…），然后靠 MX/SMTP 校验筛掉不存在的。
关键前提：人名必须是真实抓到的，绝不用编造的名字生成邮箱。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from .base import DiscoveryContext, EmailCandidate

# 常见企业邮箱命名规则，按普遍程度排序（越靠前越常见 → 置信度略高）
_PATTERNS: list[tuple[str, int]] = [
    ("{first}.{last}", 40),
    ("{first}", 34),
    ("{f}{last}", 34),
    ("{first}{last}", 30),
    ("{last}", 24),
    ("{first}_{last}", 24),
    ("{f}.{last}", 24),
    ("{first}{l}", 20),
    ("{last}.{first}", 18),
    ("{last}{f}", 16),
]


def _ascii_slug(text: str) -> str:
    """把人名转成邮箱能用的 ascii 小写：'José García' -> 'jose garcia'。"""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-zA-Z\s\-]", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip().lower()


def split_name(name: str) -> tuple[str, str] | None:
    """拆成 (first, last)。拆不出两段就返回 None（单名不生成模式，噪音太大）。"""
    slug = _ascii_slug(name)
    parts = [p for p in re.split(r"[\s\-]+", slug) if len(p) > 1]
    if len(parts) < 2:
        return None
    return parts[0], parts[-1]


def generate_for_person(
    first: str, last: str, domain: str, name: str | None = None,
    title: str | None = None,
) -> list[EmailCandidate]:
    seen: set[str] = set()
    out: list[EmailCandidate] = []
    values = {"first": first, "last": last, "f": first[:1], "l": last[:1]}
    for template, base_conf in _PATTERNS:
        local = template.format(**values)
        email = f"{local}@{domain}"
        if email in seen:
            continue
        seen.add(email)
        out.append(
            EmailCandidate(
                email=email,
                source="pattern",
                confidence=base_conf,
                verified="syntax",
                person_name=name,
                person_title=title,
                note="按人名+域名推测，发送前需校验",
            )
        )
    return out


class PatternProvider:
    """离线、免费、不依赖网络。只对已抓到的真实人名起作用。"""

    name = "pattern"

    def find(self, context: DiscoveryContext) -> Iterable[EmailCandidate]:
        if not context.domain:
            return []
        domain = context.domain.lower().lstrip("www.")
        out: list[EmailCandidate] = []
        for person in context.people:
            name = str(person.get("name") or "").strip()
            if not name:
                continue
            parts = split_name(name)
            if not parts:
                continue
            out.extend(
                generate_for_person(
                    parts[0], parts[1], domain, name=name,
                    title=person.get("title"),
                )
            )
        return out
