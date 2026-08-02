from __future__ import annotations

from customsradar.enrich.extract import (
    find_emails,
    find_phones,
    parse_html,
    pick_internal_links,
)
from customsradar.enrich.website import _normalize_url, guess_domains, is_blocked_host

HTML = """
<html><head>
  <title>Acme Fasteners — Industrial Distributor</title>
  <meta name="description" content="We supply bolts and nuts across Europe.">
  <style>.x { color: red }</style>
  <script>var tracking = 'do not extract me';</script>
</head><body>
  <h1>Acme Fasteners</h1>
  <p>We distribute DIN 933 hex bolts and DIN 934 nuts.</p>
  <a href="/about-us">About us</a>
  <a href="/products/bolts">Bolts</a>
  <a href="/contact">Contact</a>
  <a href="/blog/2024/some-post">A blog post</a>
  <a href="https://facebook.example.com/acme">Facebook</a>
  <p>Email: sales@acme.example.com or hr@acme.example.com</p>
  <p>Tel: +34 91 123 45 67</p>
</body></html>
"""


def test_parse_html_extracts_text_and_skips_scripts():
    parsed = parse_html(HTML)
    assert parsed["title"] == "Acme Fasteners — Industrial Distributor"
    assert parsed["description"].startswith("We supply bolts")
    assert "DIN 933 hex bolts" in parsed["text"]
    assert "do not extract me" not in parsed["text"]
    assert "color: red" not in parsed["text"]


def test_parse_html_survives_broken_markup():
    parsed = parse_html("<p>unclosed <b>bold <div>weird</p>")
    assert "unclosed" in parsed["text"]


def test_find_emails_filters_noise():
    emails = find_emails(
        "sales@acme.example.com noreply@acme.example.com logo@2x.png "
        "info@acme.example.com"
    )
    assert "sales@acme.example.com" in emails
    assert "info@acme.example.com" in emails
    assert "noreply@acme.example.com" not in emails
    assert not any(e.endswith(".png") for e in emails)


def test_find_phones_rejects_long_digit_strings():
    phones = find_phones("Tel: +34 91 123 45 67  Order no: 12345678901234567890")
    assert any("34 91" in p for p in phones)
    assert not any(len(p.replace(" ", "")) > 16 for p in phones)


def test_pick_internal_links_prefers_about_and_products():
    parsed = parse_html(HTML)
    links = pick_internal_links("https://acme.example.com", parsed["links"])
    assert "https://acme.example.com/about-us" in links
    assert "https://acme.example.com/products/bolts" in links
    # 外链和无关博客不该被选
    assert not any("facebook" in link for link in links)
    assert not any("/blog/" in link for link in links)


def test_pick_internal_links_ignores_other_domains():
    links = pick_internal_links(
        "https://acme.example.com",
        [("https://other.example.org/about", "About")],
    )
    assert links == []


def test_normalize_url():
    assert _normalize_url("acme.example.com") == "https://acme.example.com"
    assert _normalize_url("https://acme.example.com/") == "https://acme.example.com"
    assert _normalize_url("notadomain") is None
    assert _normalize_url("") is None
    assert _normalize_url(None) is None


def test_blocks_internal_addresses():
    """域名来自不可信的数据文件，不能让爬虫被指向内网。"""
    for url in (
        "http://localhost/x",
        "http://127.0.0.1/",
        "https://192.168.1.10/admin",
        "http://10.0.0.5",
        "http://169.254.169.254/latest/meta-data/",  # 云元数据服务
        "http://intranet.local/",
        "https://[::1]/",
        "",
    ):
        assert is_blocked_host(url) is True, url


def test_allows_normal_public_hosts():
    for url in ("https://acme.example.com/about", "http://www.acme.example.com"):
        assert is_blocked_host(url) is False, url


def test_guess_domains_drops_legal_words():
    guesses = guess_domains("Acme Fasteners Co Ltd")
    assert "acmefasteners.com" in guesses
    assert not any("ltd" in g for g in guesses)
