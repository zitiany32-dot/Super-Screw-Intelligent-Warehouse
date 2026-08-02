"""从已抓取的官网内容里取邮箱。

官网是最可信的来源，所以这些候选给高置信度。已经在爬虫环节抓过一遍，
这里只是把它包成 provider，纳入统一的聚合/打分流程。
"""

from __future__ import annotations

from typing import Iterable

from .base import DiscoveryContext, EmailCandidate, is_valid_email


class WebsiteProvider:
    name = "website"

    def find(self, context: DiscoveryContext) -> Iterable[EmailCandidate]:
        domain = (context.domain or "").lower().lstrip("www.")
        out: list[EmailCandidate] = []
        for email in context.known_emails:
            if not is_valid_email(email):
                continue
            host = email.split("@", 1)[1]
            # 同域名邮箱最可信；第三方域名（如 gmail）降一档
            confidence = 80 if domain and domain in host else 55
            out.append(
                EmailCandidate(
                    email=email,
                    source="website",
                    confidence=confidence,
                    verified="syntax",
                    note="出现在公司官网页面上",
                )
            )
        return out
