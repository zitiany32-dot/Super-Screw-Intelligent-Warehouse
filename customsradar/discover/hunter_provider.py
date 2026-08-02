"""Hunter.io 域名搜索 —— B2B 找邮箱的行业标配，合规、直接给人名+职位+置信度。

需要 HUNTER_API_KEY。这是最省事、质量最高的来源；同类还有 Apollo、Snov.io、
Clearbit，接法一样，照这个文件再写一个 provider 即可。
"""

from __future__ import annotations

import logging
from typing import Iterable

import requests

from .base import DiscoveryContext, EmailCandidate, is_valid_email

logger = logging.getLogger(__name__)


class HunterProvider:
    name = "hunter"

    def __init__(
        self,
        api_key: str,
        timeout: int = 15,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()

    def find(self, context: DiscoveryContext) -> Iterable[EmailCandidate]:
        domain = (context.domain or "").lower().lstrip("www.")
        if not self.api_key or not domain:
            return []
        try:
            resp = self.session.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": self.api_key, "limit": 20},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.info("Hunter 查询失败 %s: %s", domain, exc)
            return []

        emails = (payload.get("data") or {}).get("emails") or []
        out: list[EmailCandidate] = []
        for entry in emails:
            email = str(entry.get("value") or "").lower().strip()
            if not is_valid_email(email):
                continue
            # Hunter 自带 0-100 置信度，直接用
            confidence = int(entry.get("confidence") or 50)
            first = entry.get("first_name") or ""
            last = entry.get("last_name") or ""
            name = f"{first} {last}".strip() or None
            verification = (entry.get("verification") or {}).get("status")
            verified = "smtp_ok" if verification == "valid" else "syntax"
            out.append(
                EmailCandidate(
                    email=email,
                    source="hunter",
                    confidence=confidence,
                    verified=verified,
                    person_name=name,
                    person_title=entry.get("position"),
                    note="来自 Hunter.io 域名搜索",
                )
            )
        return out
