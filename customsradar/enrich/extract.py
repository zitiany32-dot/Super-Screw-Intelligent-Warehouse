"""HTML 文本 / 邮箱 / 链接抽取。只用标准库 html.parser，不引 bs4。"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "td", "th",
}

EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}"
)
# 常见的图片/占位假邮箱，别当成联系人
_EMAIL_NOISE = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|css|js)$|^(example|test|noreply|no-reply|donotreply)@",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{7,20}\d")

# 值得多抓一页的路径关键词（英/中/西/德/法）
INTERESTING_PATH_WORDS = (
    "about", "company", "profile", "product", "products", "catalog", "catalogue",
    "solution", "service", "industr", "contact", "quality", "certif", "team",
    "quienes", "empresa", "productos", "contacto", "ueber", "über", "unternehmen",
    "produkte", "kontakt", "apropos", "entreprise", "produits", "关于", "公司",
    "产品", "联系",
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []  # (href, anchor text)
        self.title: str = ""
        self.description: str = ""
        self._skip_depth = 0
        self._in_title = False
        self._current_href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {k.lower(): (v or "") for k, v in attrs}
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = attrs_map.get("name", "").lower()
            prop = attrs_map.get("property", "").lower()
            if name in {"description", "keywords"} or prop == "og:description":
                content = attrs_map.get("content", "").strip()
                if content and len(content) > len(self.description):
                    self.description = content
        elif tag == "a":
            href = attrs_map.get("href", "").strip()
            if href:
                self._current_href = href
                self._anchor_text = []
        elif tag == "img":
            alt = attrs_map.get("alt", "").strip()
            if alt:
                self.parts.append(alt)
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._current_href is not None:
            self.links.append((self._current_href, " ".join(self._anchor_text).strip()))
            self._current_href = None
            self._anchor_text = []
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title += text
            return
        self.parts.append(text)
        if self._current_href is not None:
            self._anchor_text.append(text)


def parse_html(html: str) -> dict:
    """解析 HTML，返回 {title, description, text, links}。"""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # 网页 HTML 千奇百怪，解析崩了就用已拿到的部分
        pass
    text = " ".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return {
        "title": parser.title.strip()[:300],
        "description": parser.description.strip()[:600],
        "text": text,
        "links": parser.links,
    }


def find_emails(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in EMAIL_RE.finditer(text or ""):
        email = match.group().strip().strip(".").lower()
        if _EMAIL_NOISE.search(email):
            continue
        seen.setdefault(email, None)
    return list(seen)[:20]


def find_phones(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in PHONE_RE.finditer(text or ""):
        phone = re.sub(r"\s{2,}", " ", match.group().strip())
        digits = re.sub(r"\D", "", phone)
        # 8-15 位才像电话；再长多半是订单号/序列号
        if 8 <= len(digits) <= 15:
            seen.setdefault(phone, None)
    return list(seen)[:10]


def pick_internal_links(
    base_url: str, links: list[tuple[str, str]], limit: int = 8
) -> list[str]:
    """挑出同域名下「关于/产品/联系」这类值得再抓一页的链接。"""
    base_host = urlparse(base_url).netloc.lower()
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    for href, anchor in links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc.lower().removeprefix("www.") != base_host.removeprefix("www."):
            continue
        clean = parsed._replace(fragment="", query="").geturl().rstrip("/")
        if not clean or clean in seen or clean.rstrip("/") == base_url.rstrip("/"):
            continue
        haystack = f"{parsed.path.lower()} {anchor.lower()}"
        score = sum(2 for word in INTERESTING_PATH_WORDS if word in haystack)
        if score == 0:
            continue
        # 路径越浅越可能是主栏目页
        score -= parsed.path.count("/")
        seen.add(clean)
        scored.append((score, clean))

    scored.sort(key=lambda item: (-item[0], len(item[1])))
    return [url for _, url in scored[:limit]]
