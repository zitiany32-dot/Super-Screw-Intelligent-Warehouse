"""搜索引擎里搜公司邮箱。

走**官方搜索 API**（SerpAPI / Bing / Brave），不直接爬 Google/百度结果页 ——
后者违反它们的 ToS，量一大就被封，正是「会被端」的做法。填了对应的 API key
才启用；没 key 就跳过。

搜索式类似：site:公司域名 (email OR contact OR "@域名")，从返回的标题+摘要里
正则抠邮箱。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

import requests

from .base import DiscoveryContext, EmailCandidate, is_valid_email

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")


class SearchProvider:
    """engine ∈ serpapi / bing / brave。key 从 config 传入。"""

    name = "search"

    def __init__(
        self,
        engine: str,
        api_key: str,
        timeout: int = 15,
        session: requests.Session | None = None,
    ) -> None:
        self.engine = (engine or "").lower()
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()

    def _queries(self, context: DiscoveryContext) -> list[str]:
        domain = (context.domain or "").lower().lstrip("www.")
        queries = []
        if domain:
            queries.append(f'"@{domain}"')
            queries.append(f"site:{domain} (email OR contact OR sales)")
        if context.company_name:
            queries.append(f'"{context.company_name}" email contact')
        return queries[:3]

    def _search(self, query: str) -> list[dict[str, Any]]:
        try:
            if self.engine == "serpapi":
                resp = self.session.get(
                    "https://serpapi.com/search",
                    params={"q": query, "api_key": self.api_key, "num": 10},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.json().get("organic_results", []) or []
            if self.engine == "bing":
                resp = self.session.get(
                    "https://api.bing.microsoft.com/v7.0/search",
                    params={"q": query, "count": 10},
                    headers={"Ocp-Apim-Subscription-Key": self.api_key},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.json().get("webPages", {}).get("value", []) or []
            if self.engine == "brave":
                resp = self.session.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": 10},
                    headers={"X-Subscription-Token": self.api_key},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.json().get("web", {}).get("results", []) or []
        except (requests.RequestException, ValueError) as exc:
            logger.info("搜索失败 [%s] %s: %s", self.engine, query, exc)
        return []

    def find(self, context: DiscoveryContext) -> Iterable[EmailCandidate]:
        if not self.api_key or self.engine not in {"serpapi", "bing", "brave"}:
            return []
        domain = (context.domain or "").lower().lstrip("www.")
        seen: set[str] = set()
        out: list[EmailCandidate] = []
        for query in self._queries(context):
            for result in self._search(query):
                blob = " ".join(
                    str(result.get(k, "") or "")
                    for k in ("title", "snippet", "description", "link", "url")
                )
                for match in _EMAIL_RE.finditer(blob):
                    email = match.group().lower().strip(".")
                    if email in seen or not is_valid_email(email):
                        continue
                    seen.add(email)
                    same_domain = bool(domain and domain in email.split("@", 1)[1])
                    out.append(
                        EmailCandidate(
                            email=email,
                            source="search",
                            confidence=58 if same_domain else 40,
                            verified="syntax",
                            note=f"搜索引擎（{self.engine}）结果里出现",
                        )
                    )
        return out[:10]
