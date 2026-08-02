"""WHOIS 注册信息里的邮箱。

域名注册时留的联系邮箱有时就是老板/管理员本人。GDPR 之后很多被隐私代理挡住
（whoisguard/redacted），挡住就跳过，不影响其他来源。

实现上优先用系统 whois 命令，没有再试 python-whois 库，都没有就静默跳过 ——
不硬依赖任何第三方包。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Iterable

from .base import DiscoveryContext, EmailCandidate, is_valid_email

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")

# 隐私代理 / 占位邮箱，命中就丢
_PRIVACY_MARKERS = (
    "privacy", "whoisguard", "redacted", "proxy", "protect", "anonymize",
    "domainsbyproxy", "contactprivacy", "withheldforprivacy", "data-protect",
    "gdpr", "notdisclosed", "abuse@", "hostmaster@", "not.disclosed",
)


def _looks_private(email: str) -> bool:
    lower = email.lower()
    return any(marker in lower for marker in _PRIVACY_MARKERS)


def _run_whois(domain: str, timeout: int) -> str:
    binary = shutil.which("whois")
    if binary:
        try:
            result = subprocess.run(
                [binary, domain],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return result.stdout or ""
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("whois 命令失败 %s: %s", domain, exc)
    try:
        import whois  # type: ignore[import-not-found]

        data = whois.whois(domain)  # 库内部也会超时/抛错
        return str(data)
    except Exception as exc:  # noqa: BLE001 — 库缺失或查询失败都跳过
        logger.debug("python-whois 不可用 %s: %s", domain, exc)
        return ""


class WhoisProvider:
    name = "whois"

    def __init__(self, timeout: int = 10) -> None:
        self.timeout = timeout

    def find(self, context: DiscoveryContext) -> Iterable[EmailCandidate]:
        if not context.domain:
            return []
        domain = context.domain.lower().lstrip("www.")
        text = _run_whois(domain, self.timeout)
        if not text:
            return []

        seen: set[str] = set()
        out: list[EmailCandidate] = []
        for match in _EMAIL_RE.finditer(text):
            email = match.group().lower().strip(".")
            if email in seen or not is_valid_email(email) or _looks_private(email):
                continue
            seen.add(email)
            # 注册邮箱在注册商域名下的（如 gmail、注册商邮箱）降一档
            same_domain = domain in email.split("@", 1)[1]
            out.append(
                EmailCandidate(
                    email=email,
                    source="whois",
                    confidence=62 if same_domain else 45,
                    verified="syntax",
                    note="来自域名 WHOIS 注册信息",
                )
            )
        return out[:5]
