"""三个 AI 环节：公司画像分析、开发信起草、回信分析+起草。"""

from __future__ import annotations

from typing import Any

from ..config import SellerProfile
from . import prompts
from .client import StructuredLLM, Usage
from .schemas import COLD_EMAIL_SCHEMA, COMPANY_ANALYSIS_SCHEMA, REPLY_ANALYSIS_SCHEMA

VALID_PRIORITIES = {"high", "medium", "low"}


def analyze_company(
    llm: StructuredLLM,
    seller: SellerProfile,
    company: dict[str, Any],
    records: list[dict[str, Any]],
    enrichment: dict[str, Any] | None,
    prescore_value: int,
    prescore_reasons: list[str],
) -> tuple[dict[str, Any], Usage]:
    data, usage = llm.structured(
        system=prompts.analysis_system_prompt(seller),
        user=prompts.analysis_user_prompt(
            company, records, enrichment, prescore_value, prescore_reasons
        ),
        schema=COMPANY_ANALYSIS_SCHEMA,
        label=f"analyze:{company.get('name')}",
    )
    return _clean_analysis(data, prescore_value), usage


def _clean_analysis(data: dict[str, Any], prescore_value: int) -> dict[str, Any]:
    priority = str(data.get("priority") or "").strip().lower()
    if priority not in VALID_PRIORITIES:
        priority = "low"
    try:
        score = int(data.get("score"))
    except (TypeError, ValueError):
        score = prescore_value
    data["priority"] = priority
    data["score"] = max(0, min(100, score))
    if not isinstance(data.get("angles"), list):
        data["angles"] = []
    if not isinstance(data.get("profile"), dict):
        data["profile"] = {}
    if not isinstance(data.get("assessment"), dict):
        data["assessment"] = {}
    if not isinstance(data.get("contacts_found"), list):
        data["contacts_found"] = []
    return data


def real_people(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """从分析结果里取出有名字的真实联系人，供邮箱模式生成用。"""
    people = []
    for contact in analysis.get("contacts_found") or []:
        name = str(contact.get("name") or "").strip()
        if name and name.lower() not in {"unknown", "n/a", "none", "-"}:
            people.append(
                {"name": name, "title": (contact.get("title") or "").strip() or None}
            )
    return people


def draft_cold_email(
    llm: StructuredLLM,
    seller: SellerProfile,
    company: dict[str, Any],
    analysis: dict[str, Any],
    records: list[dict[str, Any]],
    enrichment: dict[str, Any] | None,
) -> tuple[dict[str, Any], Usage]:
    language = _pick_language(analysis, company)
    data, usage = llm.structured(
        system=prompts.cold_email_system_prompt(seller),
        user=prompts.cold_email_user_prompt(
            company, analysis, records, enrichment, language
        ),
        schema=COLD_EMAIL_SCHEMA,
        label=f"draft:{company.get('name')}",
    )
    data.setdefault("language", language)
    return data, usage


def analyze_and_draft_reply(
    llm: StructuredLLM,
    seller: SellerProfile,
    company: dict[str, Any],
    original_draft: dict[str, Any] | None,
    reply: dict[str, Any],
    analysis: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Usage]:
    return llm.structured(
        system=prompts.reply_system_prompt(seller),
        user=prompts.reply_user_prompt(company, original_draft, reply, analysis),
        schema=REPLY_ANALYSIS_SCHEMA,
        label=f"reply:{company.get('name')}",
    )


# --------------------------------------------------------------------------- #

# 国家 → 开发信语言。名单外一律用英语，这是外贸的安全默认值。
_COUNTRY_LANGUAGE = {
    "spain": "es", "mexico": "es", "argentina": "es", "chile": "es",
    "colombia": "es", "peru": "es", "ecuador": "es", "venezuela": "es",
    "uruguay": "es", "bolivia": "es", "paraguay": "es", "costa rica": "es",
    "guatemala": "es", "dominican republic": "es", "panama": "es",
    "brazil": "pt", "portugal": "pt",
    "germany": "de", "austria": "de", "switzerland": "de",
    "france": "fr", "belgium": "fr", "senegal": "fr", "morocco": "fr",
    "italy": "it", "russia": "ru", "japan": "ja", "korea": "ko",
    "south korea": "ko", "turkey": "tr", "poland": "pl",
    "netherlands": "nl", "vietnam": "vi", "thailand": "th", "indonesia": "id",
}


def _pick_language(analysis: dict[str, Any], company: dict[str, Any]) -> str:
    """决定开发信语言：模型判断 > 官网语言 > 国家默认 > 英语。"""
    for candidate in (
        analysis.get("email_language"),
        (analysis.get("profile") or {}).get("website_language"),
    ):
        code = str(candidate or "").strip().lower()[:5]
        if code and code not in {"unknown", "n/a", "none", "zh", "zh-cn"}:
            return code
    country = (company.get("country") or "").strip().lower()
    return _COUNTRY_LANGUAGE.get(country, "en")
