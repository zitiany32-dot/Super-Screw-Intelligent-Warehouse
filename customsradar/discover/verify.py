"""邮箱校验：语法 → MX 记录 → （可选）SMTP 探测。

三档，越往后越「暴力」也越有风险：
  * syntax        —— 格式对不对，零成本
  * mx            —— 域名有没有收信服务器，一次 DNS 查询，安全
  * smtp_ok/fail  —— 连到对方邮件服务器问「这个地址存在吗」。默认关闭。

为什么 SMTP 探测默认关：频繁探测会让你的发信 IP 被对方和反垃圾服务标记成垃圾
发送源 —— 结果是你真正的开发信进不了收件箱，等于自己把自己「端」了。而且很多
企业邮箱是 catch-all（什么地址都回存在），探测结果本来就不可信。
要开就开，但要限速、要清楚代价。
"""

from __future__ import annotations

import logging
import re
import shutil
import smtplib
import socket
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_MX_RE = re.compile(r"\b\d+\s+([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")


@dataclass
class MXResult:
    ok: bool
    host: str | None = None
    method: str = "none"          # dnspython / dig / resolve / none
    note: str | None = None


def resolve_mx(domain: str, timeout: int = 8) -> MXResult:
    """查域名的 MX 记录。降级链：dnspython → dig/nslookup → A 记录解析。"""
    domain = (domain or "").strip().lower().lstrip("www.")
    if not domain:
        return MXResult(ok=False)

    # 1) dnspython（最准）
    try:
        import dns.resolver  # type: ignore[import-not-found]

        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        answers = resolver.resolve(domain, "MX")
        hosts = sorted(
            ((r.preference, str(r.exchange).rstrip(".")) for r in answers),
            key=lambda x: x[0],
        )
        if hosts:
            return MXResult(ok=True, host=hosts[0][1], method="dnspython")
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 — 无 MX 记录 / 查询失败
        logger.debug("dnspython 查 MX 失败 %s: %s", domain, exc)

    # 2) 系统 dig / nslookup / host
    host = _mx_via_cli(domain, timeout)
    if host:
        return MXResult(ok=True, host=host, method="dig")

    # 3) 兜底：域名能解析成 IP 就算「大概率能收信」，但标注未验证 MX
    try:
        socket.getaddrinfo(domain, None)
        return MXResult(
            ok=True, host=None, method="resolve", note="仅确认域名可解析，未验证 MX"
        )
    except socket.gaierror:
        return MXResult(ok=False, method="resolve", note="域名无法解析")


def _mx_via_cli(domain: str, timeout: int) -> str | None:
    for binary, args, extract in (
        ("dig", ["+short", "mx", domain], lambda o: _first_dig_mx(o)),
        ("host", ["-t", "mx", domain], lambda o: _first_re_mx(o)),
        ("nslookup", ["-type=mx", domain], lambda o: _first_re_mx(o)),
    ):
        path = shutil.which(binary)
        if not path:
            continue
        try:
            result = subprocess.run(
                [path, *args], capture_output=True, text=True,
                timeout=timeout, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        host = extract(result.stdout or "")
        if host:
            return host
    return None


def _first_dig_mx(output: str) -> str | None:
    best: tuple[int, str] | None = None
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            pref, host = int(parts[0]), parts[1].rstrip(".")
            if best is None or pref < best[0]:
                best = (pref, host)
    return best[1] if best else None


def _first_re_mx(output: str) -> str | None:
    match = _MX_RE.search(output)
    return match.group(1).rstrip(".") if match else None


def smtp_probe(
    email: str,
    mx_host: str,
    mail_from: str = "verify@example.com",
    timeout: int = 10,
    catch_all_email: str | None = None,
) -> str:
    """连到对方 MX 问某地址是否存在。返回 smtp_ok / smtp_fail / smtp_unknown。

    ⚠️ 有代价（见模块文档）。会先探一个随机地址判断是不是 catch-all，
    如果是 catch-all，任何地址都「存在」，结果不可信 → smtp_unknown。
    """
    try:
        with smtplib.SMTP(mx_host, 25, timeout=timeout) as smtp:
            smtp.ehlo_or_helo_if_needed()
            smtp.mail(mail_from)

            domain = email.split("@", 1)[1]
            probe_addr = catch_all_email or f"zzz-noexist-{socket.gethostname()[:6]}@{domain}"
            code_probe, _ = smtp.rcpt(probe_addr)
            if 200 <= code_probe < 300:
                return "smtp_unknown"  # catch-all，问什么都说有

            code, _ = smtp.rcpt(email)
            if 200 <= code < 300:
                return "smtp_ok"
            if 500 <= code < 600:
                return "smtp_fail"
            return "smtp_unknown"
    except (smtplib.SMTPException, socket.error, OSError) as exc:
        logger.info("SMTP 探测失败 %s@%s: %s", email, mx_host, exc)
        return "smtp_unknown"
