"""Apollo.io People Search —— 按公司域名找联系人 + 邮箱。

和 Hunter 一样是合规的 B2B 数据源，区别在于 Apollo 更偏「人」：按域名搜到公司里
的人，带职位，可以进一步只要采购/供应链岗位。需要 APOLLO_API_KEY。

写法完全对齐 hunter_provider.py —— 想接 Snov.io / Clearbit / 某个国内库，照这个
再抄一份、改掉 URL 和字段映射即可，其余流程（聚合/去重/打分/校验）一律不用动。

注意 Apollo 的坑：免费/低配额下 people search 返回的邮箱常是被锁的占位
（email_not_unlocked@domain.com），要「解锁」真实邮箱得调 enrichment/match 端点、
另外消耗积分。这里只取已解锁的真实邮箱，锁着的直接跳过，不替你偷偷烧积分。
"""

from __future__ import annotations

import logging
from typing import Iterable

import requests

from .base import DiscoveryContext, EmailCandidate, is_valid_email

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"

# Apollo 的 email_status → 我们的置信度 / 校验状态
_STATUS_CONFIDENCE = {"verified": 85, "likely to engage": 70, "guessed": 45}
_STATUS_VERIFIED = {"verified": "smtp_ok"}

# 锁住/占位邮箱的特征，命中就跳过（没解锁，拿到也是假的）
_LOCKED_MARKERS = ("email_not_unlocked", "email_hidden", "not_unlocked", "domain.com")

# 默认优先抓这些岗位；context 里没指定就用它缩小范围、省积分
_DEFAULT_TITLES = [
    "purchasing", "procurement", "sourcing", "buyer", "supply chain",
    "import", "owner", "founder", "ceo", "general manager", "sales",
]


class ApolloProvider:
    name = "apollo"

    def __init__(
        self,
        api_key: str,
        timeout: int = 15,
        per_page: int = 25,
        person_titles: list[str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.per_page = per_page
        self.person_titles = person_titles or _DEFAULT_TITLES
        self.session = session or requests.Session()

    def find(self, context: DiscoveryContext) -> Iterable[EmailCandidate]:
        domain = (context.domain or "").lower().lstrip("www.")
        if not self.api_key or not domain:
            return []

        body = {
            "q_organization_domains": domain,   # 可换行分隔多个域名
            "page": 1,
            "per_page": self.per_page,
            "person_titles": self.person_titles,
        }
        try:
            resp = self.session.post(
                _SEARCH_URL,
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Cache-Control": "no-cache",
                    "X-Api-Key": self.api_key,   # 现行鉴权方式（旧版是 body 里塞 api_key）
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.info("Apollo 查询失败 %s: %s", domain, exc)
            return []

        people = payload.get("people") or payload.get("contacts") or []
        out: list[EmailCandidate] = []
        for person in people:
            email = str(person.get("email") or "").lower().strip()
            if not is_valid_email(email) or any(m in email for m in _LOCKED_MARKERS):
                continue
            # 只留同域名邮箱，避免把对方的个人 gmail 之类也当公司联系人
            if domain not in email.split("@", 1)[1]:
                continue

            status = str(person.get("email_status") or "").lower()
            name = (person.get("name") or "").strip() or (
                f"{person.get('first_name') or ''} {person.get('last_name') or ''}".strip()
                or None
            )
            out.append(
                EmailCandidate(
                    email=email,
                    source="apollo",
                    confidence=_STATUS_CONFIDENCE.get(status, 55),
                    verified=_STATUS_VERIFIED.get(status, "syntax"),
                    person_name=name,
                    person_title=person.get("title"),
                    note=f"来自 Apollo.io（email_status={status or '未知'}）",
                )
            )
        return out
