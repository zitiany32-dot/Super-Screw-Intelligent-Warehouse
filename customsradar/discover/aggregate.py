"""聚合所有来源的候选邮箱：跑 provider → 合并去重 → 校验 → 打分排序。

调用方拿到一个排好序的候选列表，最上面那个就是最该用的收件人。
所有候选都带来源，没有任何一个是凭空编的 —— 这是「不发信到编造地址」的基础。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from ..config import Config
from .base import DiscoveryContext, EmailCandidate, EmailProvider
from .patterns import PatternProvider
from .verify import resolve_mx, smtp_probe
from .website_provider import WebsiteProvider

logger = logging.getLogger(__name__)

# 校验状态对最终得分的加成
_VERIFY_BONUS = {
    "smtp_ok": 25,
    "mx": 8,
    "domain_resolves": 3,
    "smtp_unknown": 0,
    "syntax": 0,
    "smtp_fail": -80,   # 明确不存在，基本废掉
}
# 邮箱类型加成：业务邮箱最想要
_ROLE_BONUS = {"business": 12, "personal": 8, "generic": 0, "low": -18}
_SOURCE_ORDER = {"hunter": 0, "website": 1, "whois": 2, "search": 3, "pattern": 4}


@dataclass
class DiscoveryResult:
    candidates: list[EmailCandidate]

    @property
    def best(self) -> EmailCandidate | None:
        return self.candidates[0] if self.candidates else None

    def usable(self, min_confidence: int = 45) -> EmailCandidate | None:
        """够可信、可以直接用的最佳候选。都不够则返回 None（交人工补）。"""
        for cand in self.candidates:
            if cand.verified == "smtp_fail":
                continue
            if cand.confidence >= min_confidence or cand.verified == "smtp_ok":
                return cand
        return None


def build_providers(config: Config) -> list[EmailProvider]:
    """按 config 决定启用哪些来源。免费的默认开，付费/联网的要 key 或开关。"""
    providers: list[EmailProvider] = [WebsiteProvider(), PatternProvider()]

    if config.discover_whois:
        from .whois_provider import WhoisProvider

        providers.append(WhoisProvider(timeout=config.discover_timeout))
    if config.hunter_api_key:
        from .hunter_provider import HunterProvider

        providers.append(HunterProvider(config.hunter_api_key, config.discover_timeout))
    if config.search_api_key and config.search_engine:
        from .search_provider import SearchProvider

        providers.append(
            SearchProvider(
                config.search_engine, config.search_api_key, config.discover_timeout
            )
        )
    return providers


def _merge(candidates: list[EmailCandidate]) -> list[EmailCandidate]:
    """按邮箱去重：留置信度最高的那条，合并所有命中的来源。"""
    by_email: dict[str, EmailCandidate] = {}
    for cand in candidates:
        if not cand.valid:
            continue
        existing = by_email.get(cand.email)
        if existing is None:
            by_email[cand.email] = cand
            continue
        # 多来源命中同一个邮箱 → 更可信，取高置信度并加一点交叉验证分
        winner = existing if existing.confidence >= cand.confidence else cand
        loser = cand if winner is existing else existing
        for src in loser.sources:
            if src not in winner.sources:
                winner.sources.append(src)
        winner.confidence = min(100, winner.confidence + 6)
        if not winner.person_name and loser.person_name:
            winner.person_name = loser.person_name
            winner.person_title = loser.person_title
        by_email[cand.email] = winner
    return list(by_email.values())


def _final_score(cand: EmailCandidate) -> int:
    score = cand.confidence
    score += _VERIFY_BONUS.get(cand.verified, 0)
    score += _ROLE_BONUS.get(cand.role, 0)
    if len(cand.sources) > 1:
        score += 5 * (len(cand.sources) - 1)
    return max(0, min(100, score))


def discover_emails(
    context: DiscoveryContext,
    config: Config,
    providers: Iterable[EmailProvider] | None = None,
) -> DiscoveryResult:
    provider_list = list(providers) if providers is not None else build_providers(config)

    raw: list[EmailCandidate] = []
    for provider in provider_list:
        try:
            found = list(provider.find(context))
            logger.debug("来源 %s 命中 %d 个候选", provider.name, len(found))
            raw.extend(found)
        except Exception as exc:  # noqa: BLE001 — 单个来源挂了不影响其他
            logger.info("来源 %s 出错: %s", getattr(provider, "name", "?"), exc)

    merged = _merge(raw)
    if config.verify_mx or config.verify_smtp:
        _verify(merged, config)

    merged.sort(
        key=lambda c: (
            -_final_score(c),
            _SOURCE_ORDER.get(c.source, 9),
            c.email,
        )
    )
    # 把最终分写回 confidence，方便展示和存库
    for cand in merged:
        cand.confidence = _final_score(cand)
    return DiscoveryResult(candidates=merged)


def _verify(candidates: list[EmailCandidate], config: Config) -> None:
    """按域名做一次 MX 查询（缓存），可选再对高价值候选做 SMTP 探测。"""
    mx_cache: dict[str, object] = {}
    smtp_budget = config.verify_smtp_max

    for cand in candidates:
        if cand.verified in {"smtp_ok", "smtp_fail"}:
            continue  # provider 已给出结果（如 Hunter）
        domain = cand.email.split("@", 1)[1]
        mx = mx_cache.get(domain)
        if mx is None:
            mx = resolve_mx(domain, timeout=config.discover_timeout)
            mx_cache[domain] = mx

        if not mx.ok:  # type: ignore[attr-defined]
            cand.verified = "smtp_fail"
            cand.note = (cand.note or "") + " | 域名无收信服务器"
            continue
        cand.verified = "mx" if mx.method != "resolve" else "domain_resolves"  # type: ignore[attr-defined]

        if (
            config.verify_smtp
            and smtp_budget > 0
            and mx.host  # type: ignore[attr-defined]
            and cand.confidence >= config.verify_smtp_min_confidence
        ):
            smtp_budget -= 1
            status = smtp_probe(
                cand.email,
                mx.host,  # type: ignore[attr-defined]
                mail_from=config.smtp_from or "verify@example.com",
                timeout=config.discover_timeout,
            )
            if status != "smtp_unknown":
                cand.verified = status
