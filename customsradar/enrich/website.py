"""爬目标公司官网：首页 + 关于/产品/联系页，抽正文和联系方式。

礼貌抓取的几条硬规矩：
  * 遵守 robots.txt（可关，但默认开）
  * 每次请求之间 sleep，串行不并发
  * 带可识别的 User-Agent
  * 只抓公开页面，不登录、不绕验证码、不抓 PDF/大文件
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .extract import find_emails, find_phones, parse_html, pick_internal_links

logger = logging.getLogger(__name__)

MAX_BYTES = 2_000_000        # 单页最多读 2MB
MAX_TEXT_CHARS = 12_000      # 汇总给 AI 的正文上限，控成本


@dataclass
class CrawlResult:
    status: str = "ok"                 # ok / no_site / blocked / error
    homepage_url: str | None = None
    pages: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "homepage_url": self.homepage_url,
            "pages": self.pages,
            "text": self.text,
            "emails": self.emails,
            "phones": self.phones,
            "error": self.error,
        }


class WebsiteCrawler:
    def __init__(
        self,
        user_agent: str,
        timeout: int = 15,
        max_pages: int = 5,
        delay: float = 1.0,
        respect_robots: bool = True,
        session: requests.Session | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_pages = max_pages
        self.delay = delay
        self.respect_robots = respect_robots
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en;q=0.9",
            }
        )
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # ------------------------------------------------------------------ #
    # robots
    # ------------------------------------------------------------------ #
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root in self._robots_cache:
            return self._robots_cache[root]
        parser = urllib.robotparser.RobotFileParser()
        try:
            response = self.session.get(
                urljoin(root, "/robots.txt"), timeout=self.timeout
            )
            if response.status_code == 200 and response.text:
                parser.parse(response.text.splitlines())
            else:
                parser = None  # 没有 robots.txt 视为不限制
        except requests.RequestException:
            parser = None
        self._robots_cache[root] = parser
        return parser

    def can_fetch(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True

    # ------------------------------------------------------------------ #
    # 单页抓取
    # ------------------------------------------------------------------ #
    def fetch_page(self, url: str) -> dict[str, Any] | None:
        if is_blocked_host(url):
            # 域名来自海关数据文件，属于不可信输入 —— 不能让它把爬虫指向内网
            logger.warning("拒绝抓取内网/本机地址: %s", url)
            return None
        if not self.can_fetch(url):
            logger.info("robots.txt 禁止抓取: %s", url)
            return None
        try:
            response = self.session.get(
                url, timeout=self.timeout, allow_redirects=True, stream=True
            )
        except requests.RequestException as exc:
            logger.info("抓取失败 %s: %s", url, exc)
            return None

        try:
            if response.status_code >= 400:
                return None
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "html" not in content_type:
                return None
            raw = response.raw.read(MAX_BYTES, decode_content=True) or b""
        except requests.RequestException as exc:
            logger.info("读取正文失败 %s: %s", url, exc)
            return None
        finally:
            response.close()

        # 注意：正文已经从 response.raw 直接读走了，这时候再碰 apparent_encoding
        # 会去读空的 response.content，所以只用响应头里的编码，拿不到就按 utf-8 解。
        encoding = response.encoding or "utf-8"
        try:
            html = raw.decode(encoding, errors="replace")
        except LookupError:  # 服务器给了个不存在的 charset
            html = raw.decode("utf-8", errors="replace")
        parsed = parse_html(html)
        return {
            "url": response.url,
            "title": parsed["title"],
            "description": parsed["description"],
            "text": parsed["text"],
            "links": parsed["links"],
        }

    # ------------------------------------------------------------------ #
    # 整站抓取
    # ------------------------------------------------------------------ #
    def crawl(self, website: str | None, domain: str | None = None) -> CrawlResult:
        start_url = _normalize_url(website or domain)
        if not start_url:
            return CrawlResult(status="no_site", error="没有官网或域名")

        homepage = self.fetch_page(start_url)
        if homepage is None and start_url.startswith("https://"):
            time.sleep(self.delay)
            homepage = self.fetch_page("http://" + start_url[len("https://"):])
        if homepage is None:
            return CrawlResult(
                status="blocked", homepage_url=start_url, error="首页抓不到（超时/被拒/无此站）"
            )

        result = CrawlResult(status="ok", homepage_url=homepage["url"])
        collected = [homepage]

        for url in pick_internal_links(homepage["url"], homepage["links"]):
            if len(collected) >= self.max_pages:
                break
            time.sleep(self.delay)
            page = self.fetch_page(url)
            if page:
                collected.append(page)

        chunks: list[str] = []
        for page in collected:
            result.pages.append(
                {
                    "url": page["url"],
                    "title": page["title"],
                    "chars": len(page["text"]),
                }
            )
            header = f"### {page['title'] or page['url']}\n{page['url']}"
            body = page["text"]
            if page["description"]:
                body = f"{page['description']}\n{body}"
            chunks.append(f"{header}\n{body}")

        full_text = "\n\n".join(chunks)
        result.text = full_text[:MAX_TEXT_CHARS]
        result.emails = find_emails(full_text)
        result.phones = find_phones(full_text)
        return result


_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}
_BLOCKED_SUFFIXES = (".local", ".internal", ".localdomain")


def is_blocked_host(url: str) -> bool:
    """挡掉指向本机/内网/云元数据服务的地址。

    公司域名来自海关数据导出文件，是不可信输入。有人（或者只是一行脏数据）
    把 website 写成 127.0.0.1 或 169.254.169.254，爬虫就会去打内网。
    这里只做字面判断，不做 DNS 解析 —— 便宜，且覆盖绝大多数情况。
    """
    import ipaddress

    host = (urlparse(url).hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # 普通域名，放行
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not text.startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return text.rstrip("/")


def guess_domains(company_name: str, tld_hints: tuple[str, ...] = ("com", "net")) -> list[str]:
    """从公司名猜几个候选域名。命中率不高，只在没有域名时兜底用。"""
    import re

    words = re.findall(r"[a-z0-9]+", (company_name or "").lower())
    stop = {
        "co", "ltd", "limited", "llc", "inc", "corp", "corporation", "gmbh",
        "srl", "sa", "bv", "nv", "oy", "ab", "plc", "pty", "the", "and", "of",
        "group", "international", "trading", "company",
    }
    core = [w for w in words if w not in stop and len(w) > 1]
    if not core:
        return []
    candidates = ["".join(core[:2]), "".join(core[:1]), "-".join(core[:2])]
    out: list[str] = []
    for base in dict.fromkeys(c for c in candidates if len(c) >= 4):
        for tld in tld_hints:
            out.append(f"{base}.{tld}")
    return out[:6]
