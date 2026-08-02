from __future__ import annotations

from customsradar.discover import (
    DiscoveryContext,
    DiscoveryResult,
    EmailCandidate,
    classify_role,
    discover_emails,
    generate_for_person,
    is_valid_email,
    split_name,
)
from customsradar.discover.aggregate import _final_score, _merge
from customsradar.discover.base import DiscoveryContext as Ctx
from customsradar.discover.patterns import PatternProvider
from customsradar.discover.website_provider import WebsiteProvider
from customsradar.discover.whois_provider import _looks_private


def test_is_valid_email():
    assert is_valid_email("sales@acme.example.com")
    assert not is_valid_email("nope")
    assert not is_valid_email("a@b")


def test_classify_role():
    assert classify_role("sales@x.com") == "business"
    assert classify_role("hr@x.com") == "low"
    assert classify_role("john.smith@x.com") == "personal"


def test_split_name():
    assert split_name("John Smith") == ("john", "smith")
    assert split_name("José García López") == ("jose", "lopez")  # 去重音、取首尾
    assert split_name("Madonna") is None  # 单名不生成模式


def test_generate_for_person_covers_common_patterns():
    cands = generate_for_person("john", "smith", "acme.com", name="John Smith")
    emails = {c.email for c in cands}
    assert "john.smith@acme.com" in emails
    assert "jsmith@acme.com" in emails
    assert "john@acme.com" in emails
    assert all(c.source == "pattern" for c in cands)
    assert all(c.person_name == "John Smith" for c in cands)


def test_website_provider_prefers_same_domain():
    ctx = Ctx(
        company_name="Acme",
        domain="acme.example.com",
        known_emails=["sales@acme.example.com", "someone@gmail.example.com"],
    )
    cands = list(WebsiteProvider().find(ctx))
    same = next(c for c in cands if "acme.example.com" in c.email)
    other = next(c for c in cands if "gmail" in c.email)
    assert same.confidence > other.confidence


def test_pattern_provider_uses_real_people_only():
    ctx = Ctx(
        company_name="Acme",
        domain="acme.example.com",
        people=[{"name": "Jane Doe", "title": "Purchasing Manager"}],
    )
    cands = list(PatternProvider().find(ctx))
    assert cands
    assert all(c.email.endswith("@acme.example.com") for c in cands)
    assert any("jane" in c.email for c in cands)


def test_pattern_provider_empty_without_domain():
    ctx = Ctx(company_name="Acme", people=[{"name": "Jane Doe"}])
    assert list(PatternProvider().find(ctx)) == []


def test_whois_privacy_filter():
    assert _looks_private("contact@whoisguard.com")
    assert _looks_private("redacted-for-privacy@example.com")
    assert not _looks_private("boss@acme.example.com")


def test_merge_dedupes_and_combines_sources():
    cands = [
        EmailCandidate("sales@acme.com", "website", confidence=80),
        EmailCandidate("sales@acme.com", "hunter", confidence=70),
        EmailCandidate("info@acme.com", "search", confidence=50),
    ]
    merged = _merge(cands)
    assert len(merged) == 2
    sales = next(c for c in merged if c.email == "sales@acme.com")
    assert set(sales.sources) == {"website", "hunter"}
    assert sales.confidence >= 80  # 交叉验证加分


def test_final_score_rewards_verification_and_business_role():
    verified_biz = EmailCandidate("sales@acme.com", "website", confidence=60, verified="smtp_ok")
    unverified_low = EmailCandidate("hr@acme.com", "website", confidence=60, verified="syntax")
    assert _final_score(verified_biz) > _final_score(unverified_low)


def test_final_score_kills_smtp_fail():
    dead = EmailCandidate("ghost@acme.com", "pattern", confidence=60, verified="smtp_fail")
    assert _final_score(dead) < 20


def test_discovery_result_usable_threshold():
    result = DiscoveryResult(
        candidates=[
            EmailCandidate("guess@acme.com", "pattern", confidence=20, verified="syntax"),
        ]
    )
    assert result.best is not None
    assert result.usable(min_confidence=45) is None  # 太不可信，不直接用


def test_discover_emails_offline_website_and_pattern(config):
    """不联网、无 key 时，只跑官网 + 人名模式两个离线来源。"""
    config.discover_whois = False
    config.verify_mx = False
    config.verify_smtp = False
    ctx = DiscoveryContext(
        company_name="Acme Fasteners",
        domain="acme.example.com",
        known_emails=["sales@acme.example.com"],
        people=[{"name": "Jane Doe", "title": "Buyer"}],
    )
    result = discover_emails(ctx, config)
    emails = {c.email for c in result.candidates}
    assert "sales@acme.example.com" in emails          # 官网
    assert any("jane" in e for e in emails)            # 人名模式
    # 官网直得的邮箱应排在推测邮箱前面
    assert result.candidates[0].email == "sales@acme.example.com"


def test_discover_emails_with_fake_provider(config):
    class FakeHunter:
        name = "hunter"

        def find(self, context):
            return [
                EmailCandidate(
                    "jane.doe@acme.example.com", "hunter", confidence=95,
                    verified="smtp_ok", person_name="Jane Doe", person_title="Buyer",
                )
            ]

    ctx = DiscoveryContext(company_name="Acme", domain="acme.example.com")
    result = discover_emails(ctx, config, providers=[FakeHunter()])
    assert result.best.email == "jane.doe@acme.example.com"
    assert result.usable() is not None


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")


class _FakeSession:
    """记录请求、返回预置响应，替掉真正的 requests.Session。"""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self.payload, self.status)


_APOLLO_PAYLOAD = {
    "people": [
        {
            "name": "Jane Doe", "first_name": "Jane", "last_name": "Doe",
            "title": "Purchasing Manager", "email": "jane.doe@acme.example.com",
            "email_status": "verified",
        },
        {
            "name": "Locked Person", "title": "Buyer",
            "email": "email_not_unlocked@domain.com", "email_status": "verified",
        },
        {
            "name": "Guess Guy", "title": "Sourcing",
            "email": "g.guy@acme.example.com", "email_status": "guessed",
        },
        {
            "name": "Third Party", "title": "Consultant",
            "email": "someone@gmail.example.com", "email_status": "verified",
        },
    ]
}


def test_apollo_provider_maps_people_to_candidates():
    from customsradar.discover.apollo_provider import ApolloProvider

    session = _FakeSession(_APOLLO_PAYLOAD)
    provider = ApolloProvider("fake-key", session=session)
    ctx = Ctx(company_name="Acme", domain="acme.example.com")
    cands = list(provider.find(ctx))
    emails = {c.email: c for c in cands}

    # 验证过的真实邮箱进来了，置信度高、标了 smtp_ok
    assert "jane.doe@acme.example.com" in emails
    assert emails["jane.doe@acme.example.com"].verified == "smtp_ok"
    assert emails["jane.doe@acme.example.com"].person_title == "Purchasing Manager"
    # 猜测的进来但置信度低
    assert "g.guy@acme.example.com" in emails
    assert emails["g.guy@acme.example.com"].confidence < 55
    # 锁住的占位邮箱被跳过
    assert not any("email_not_unlocked" in e for e in emails)
    # 非本域名的第三方邮箱被跳过
    assert "someone@gmail.example.com" not in emails
    # 鉴权头带上了
    assert session.calls[0]["headers"]["X-Api-Key"] == "fake-key"


def test_apollo_provider_skips_without_key_or_domain():
    from customsradar.discover.apollo_provider import ApolloProvider

    provider = ApolloProvider("", session=_FakeSession(_APOLLO_PAYLOAD))
    assert list(provider.find(Ctx(company_name="Acme", domain="acme.example.com"))) == []

    provider2 = ApolloProvider("k", session=_FakeSession(_APOLLO_PAYLOAD))
    assert list(provider2.find(Ctx(company_name="Acme", domain=None))) == []


def test_apollo_provider_survives_error_response():
    from customsradar.discover.apollo_provider import ApolloProvider

    provider = ApolloProvider("k", session=_FakeSession({}, status=500))
    assert list(provider.find(Ctx(company_name="Acme", domain="acme.example.com"))) == []


def test_apollo_wired_into_build_providers(config):
    from customsradar.discover.aggregate import build_providers

    config.apollo_api_key = "fake-key"
    names = {getattr(p, "name", "?") for p in build_providers(config)}
    assert "apollo" in names


def test_classify_mx_normal():
    from customsradar.discover.verify import classify_mx

    r = classify_mx([(20, "mx2.acme.com"), (10, "mx1.acme.com")])
    assert r.ok and r.host == "mx1.acme.com"  # 取优先级最低（最优）的


def test_classify_mx_null_mx_means_no_mail():
    """RFC 7505 null MX（唯一 MX 是 '.'）表示该域名不收信，必须判为不可投递。"""
    from customsradar.discover.verify import classify_mx

    r = classify_mx([(0, "")])
    assert r.ok is False
    assert "null MX" in (r.note or "")


def test_classify_mx_empty():
    from customsradar.discover.verify import classify_mx

    assert classify_mx([]).ok is False


def test_null_mx_candidate_gets_killed_in_verification(config):
    """null MX 域名下的候选邮箱应被校验标成 smtp_fail、排到最后。"""
    from unittest.mock import patch

    from customsradar.discover import DiscoveryContext, EmailCandidate
    from customsradar.discover.verify import MXResult

    config.verify_mx = True
    config.verify_smtp = False
    ctx = DiscoveryContext(company_name="Dead", domain="nomail.example")

    class Prov:
        name = "website"

        def find(self, c):
            return [EmailCandidate("info@nomail.example", "website", confidence=80)]

    with patch(
        "customsradar.discover.aggregate.resolve_mx",
        return_value=MXResult(ok=False, note="null MX（RFC 7505），该域名不收信"),
    ):
        result = discover_emails(ctx, config, providers=[Prov()])
    assert result.candidates[0].verified == "smtp_fail"
    assert result.usable() is None  # 不可投递，不该拿来发信


def test_broken_provider_does_not_crash_discovery(config):
    class BrokenProvider:
        name = "broken"

        def find(self, context):
            raise RuntimeError("boom")

    good = EmailCandidate("sales@acme.example.com", "website", confidence=80)

    class GoodProvider:
        name = "good"

        def find(self, context):
            return [good]

    ctx = DiscoveryContext(company_name="Acme", domain="acme.example.com")
    result = discover_emails(ctx, config, providers=[BrokenProvider(), GoodProvider()])
    assert result.best.email == "sales@acme.example.com"
