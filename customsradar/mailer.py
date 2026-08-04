"""发信。整个系统里唯一会往外发东西的地方。

设计上有三道闸，缺一封都发不出去：
  1. 草稿状态必须是 approved —— 而 approved 只能由人在后台点出来
  2. 配置里 RADAR_ALLOW_SEND 必须为 true
  3. send_draft() 必须显式传 confirm=True

流水线代码不会、也不应该调用这里的任何发送函数。
"""

from __future__ import annotations

import logging
import re
import smtplib
import sqlite3
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any

from . import store
from .config import Config
from .db import utcnow

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}$")
# 正文里没填完的占位符，带着这个发出去会很尴尬
_PLACEHOLDER_RE = re.compile(r"\[\[待确认[:：][^\]]*\]\]")


class SendBlocked(RuntimeError):
    """发送被闸门拦下（未审核、未开开关、正文有占位符等）。"""


@dataclass
class SendResult:
    draft_id: int
    message_id: str
    to_email: str
    sent_at: str


# --------------------------------------------------------------------------- #
# 审核动作（人来点）
# --------------------------------------------------------------------------- #

def approve_draft(
    conn: sqlite3.Connection,
    draft_id: int,
    subject: str | None = None,
    body: str | None = None,
    to_email: str | None = None,
) -> dict[str, Any]:
    """人工审核通过。可以顺带把修改后的正文一起存进去。"""
    draft = store.get_draft(conn, draft_id)
    if draft is None:
        raise SendBlocked(f"草稿 #{draft_id} 不存在")
    if draft["status"] == "sent":
        raise SendBlocked(f"草稿 #{draft_id} 已经发送过了")

    updates: dict[str, Any] = {"status": "approved", "reviewed_at": utcnow()}
    if subject is not None:
        updates["subject"] = subject
    if body is not None:
        updates["body"] = body
    if to_email is not None:
        updates["to_email"] = to_email.strip()
    store.update_draft(conn, draft_id, **updates)
    conn.commit()
    return store.get_draft(conn, draft_id)  # type: ignore[return-value]


def reject_draft(
    conn: sqlite3.Connection, draft_id: int, reason: str = ""
) -> dict[str, Any]:
    store.update_draft(
        conn, draft_id, status="rejected", reviewed_at=utcnow(), error=reason or None
    )
    conn.commit()
    return store.get_draft(conn, draft_id)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 发送前检查
# --------------------------------------------------------------------------- #

def preflight(config: Config, draft: dict[str, Any]) -> list[str]:
    """返回所有拦截理由。空列表 = 可以发。"""
    problems: list[str] = []
    if draft["status"] != "approved":
        problems.append(
            f"草稿状态是 {draft['status']}，只有 approved 的草稿才能发送"
        )
    to_email = (draft.get("to_email") or "").strip()
    if not to_email:
        problems.append("没有收件人")
    elif not _EMAIL_RE.match(to_email):
        problems.append(f"收件人邮箱格式不对: {to_email}")
    if not (draft.get("subject") or "").strip():
        problems.append("主题为空")
    body = draft.get("body") or ""
    if not body.strip():
        problems.append("正文为空")
    placeholders = _PLACEHOLDER_RE.findall(body)
    if placeholders:
        problems.append(f"正文里还有未填的占位符: {'; '.join(placeholders[:3])}")
    if not config.allow_send:
        problems.append("RADAR_ALLOW_SEND 未打开")
    if not config.smtp_host:
        problems.append("没有配置 SMTP_HOST")
    if not sender_address(config):
        problems.append("没有配置发件邮箱（SELLER_SENDER_EMAIL 或 SMTP_FROM）")
    return problems


# --------------------------------------------------------------------------- #
# 组装 & 发送
# --------------------------------------------------------------------------- #

def sender_address(config: Config) -> str:
    """发件邮箱：优先 SELLER_SENDER_EMAIL，其次 SMTP_FROM。都没有就返回空串。

    分开两个来源是为了「不配 SMTP、只导出 .eml 手动发」这条路 —— 那种用法下
    根本没有 SMTP_FROM，但签名和 From 仍然需要一个真实邮箱。
    """
    return (config.seller.sender_email or config.smtp_from or "").strip()


def build_message(
    config: Config,
    draft: dict[str, Any],
    message_id: str | None = None,
    for_export: bool = False,
) -> EmailMessage:
    """组装邮件。

    for_export=True 用于导出 .eml 手动发送：
      * 不知道发件邮箱时**不写 From**，让邮件客户端自动填你的默认账号
        （写成 `名字 <>` 是非法地址，Outlook/Foxmail 会报错或判垃圾）
      * 不预置 Message-ID —— 客户端发送时会自己生成，预置的反而会被替换掉，
        还可能带上 @localhost 这种一看就是机器发的域名
    """
    msg = EmailMessage()
    address = sender_address(config)
    sender_name = config.seller.sender_name or config.seller.company_name

    if address:
        msg["From"] = formataddr((sender_name, address))
    elif not for_export:
        # 真发信没有发件地址是配置错误，preflight 会先拦下，这里兜底
        raise SendBlocked("没有配置发件邮箱（SELLER_SENDER_EMAIL 或 SMTP_FROM）")

    msg["To"] = draft["to_email"] or ""
    msg["Subject"] = draft["subject"]
    msg["Date"] = formatdate(localtime=True)

    if not for_export:
        msg["Message-ID"] = message_id or make_msgid(
            domain=address.split("@")[-1] if "@" in address else None
        )
    if draft.get("in_reply_to"):
        msg["In-Reply-To"] = draft["in_reply_to"]
        msg["References"] = draft["in_reply_to"]

    body = draft["body"]
    signature = _signature(config)
    msg.set_content(f"{body}\n\n{signature}" if signature else body)
    return msg


def _signature(config: Config) -> str:
    seller = config.seller
    lines = [line for line in (
        "--",
        seller.sender_name,
        seller.sender_title if seller.sender_name else "",
        seller.company_name,
        seller.website,
        seller.sender_phone,
        sender_address(config),
    ) if line]
    return "\n".join(lines) if len(lines) > 1 else ""


def send_draft(
    conn: sqlite3.Connection,
    config: Config,
    draft_id: int,
    confirm: bool = False,
    smtp_factory: Any | None = None,
) -> SendResult:
    """真正发出去。confirm 必须显式传 True —— 防手滑，也防被别的代码顺手调用。"""
    if not confirm:
        raise SendBlocked("send_draft 需要显式 confirm=True")

    draft = store.get_draft(conn, draft_id)
    if draft is None:
        raise SendBlocked(f"草稿 #{draft_id} 不存在")
    if draft["status"] == "sent":
        raise SendBlocked(f"草稿 #{draft_id} 已发送，不重复发送")

    problems = preflight(config, draft)
    if problems:
        raise SendBlocked("；".join(problems))

    message = build_message(config, draft)
    message_id = message["Message-ID"]

    try:
        _deliver(config, message, smtp_factory)
    except Exception as exc:  # noqa: BLE001
        store.update_draft(conn, draft_id, status="failed", error=str(exc))
        conn.commit()
        logger.exception("发送草稿 #%s 失败", draft_id)
        raise

    sent_at = utcnow()
    store.update_draft(
        conn,
        draft_id,
        status="sent",
        sent_at=sent_at,
        message_id=message_id,
        error=None,
    )
    store.set_company_status(conn, draft["company_id"], "contacted")
    conn.commit()
    logger.info("已发送草稿 #%s → %s", draft_id, draft["to_email"])
    return SendResult(
        draft_id=draft_id,
        message_id=message_id,
        to_email=draft["to_email"],
        sent_at=sent_at,
    )


def _deliver(config: Config, message: EmailMessage, smtp_factory: Any | None) -> None:
    if smtp_factory is not None:  # 测试注入点
        with smtp_factory() as smtp:
            smtp.send_message(message)
        return

    if config.smtp_port == 465:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=30) as smtp:
            if config.smtp_user:
                smtp.login(config.smtp_user, config.smtp_password)
            smtp.send_message(message)
        return

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
        smtp.ehlo()
        if config.smtp_starttls:
            smtp.starttls()
            smtp.ehlo()
        if config.smtp_user:
            smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)


def export_eml(config: Config, draft: dict[str, Any], out_dir: Path) -> Path:
    """导出成 .eml，可以直接拖进 Outlook/Foxmail 手动发 —— 不想配 SMTP 时用这个。

    注意：手动发出去的信，Message-ID 由你的邮件客户端生成，跟数据库里对不上，
    所以 inbox 收回复时走的是「按发件邮箱/域名匹配公司」这条兜底路径。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    message = build_message(config, draft, for_export=True)
    path = out_dir / f"draft-{draft['id']}.eml"
    path.write_bytes(bytes(message))
    return path
