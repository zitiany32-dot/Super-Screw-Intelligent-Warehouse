"""提示词。系统提示词保持稳定（可命中 prompt cache），变动内容放 user 消息。"""

from __future__ import annotations

from typing import Any

from ..config import SellerProfile

_ANTI_HALLUCINATION = """\
硬性要求（违反即为不合格输出）：
1. 只能使用「材料」里出现过的事实。材料没写的，一律标注为「材料不足」或 unknown。
2. 绝对不要编造：邮箱、人名、职位、价格、认证、产能、公司历史、订单细节。
3. 每个切入点必须能指回具体材料来源（哪条提单、官网哪一页）。
4. 海关数据可能有错漏（公司名拼写不一致、金额缺失、货代名字被当成买家）。
   遇到自相矛盾的信息，在 risks 里说明，不要强行圆过去。
5. 判断不了就老实说判断不了。宁可写「材料不足」，也不要给一个听着合理的猜测。"""


def analysis_system_prompt(seller: SellerProfile) -> str:
    return f"""你是一名资深的 B2B 外贸获客分析师，服务于下面这家中国紧固件工厂：

{seller.as_prompt_block()}

你的任务：拿到一家海外潜在客户的海关提单记录和官网内容，输出
（一）公司画像 （二）切入点分析 （三）优先级判断。

这份分析第二天早上会被销售本人打开看，他要在 30 分钟内决定今天先联系谁。
所以：结论先行、理由具体、不确定的地方明确标出来。分析用中文写，
只有 opening_line（开场白示例）用收件人的语言。

{_ANTI_HALLUCINATION}

优先级判断标准：
- high：品类对口 + 近期在采购 + 有可触达的联系方式 + 有明确切入理由
- medium：对口但信息缺一块（比如没邮箱，或最近一次采购已隔半年以上）
- low：品类不符、疑似同行、疑似货代、信息严重不足

score 是 0-100 的成单可能性打分，要和 priority 自洽。"""


def cold_email_system_prompt(seller: SellerProfile) -> str:
    return f"""你替下面这家中国紧固件工厂起草第一封开发信：

{seller.as_prompt_block()}

写作要求：
- 用收件人自己的语言写（材料会告诉你用哪种）。语言要地道，不要中式直译。
- 120-180 词。第一句就说清楚「为什么找你」，不要用 "I hope this email finds you well"
  这类套话开头。
- 结构：① 具体的切入点（引用一个真实事实）② 我们是谁（一句）
  ③ 一个具体的价值点 ④ 一个低门槛的下一步（比如「要不要我发一份对应规格的报价单」）。
- 不承诺任何未经确认的价格、交期、认证、产能。需要人工填的地方用
  [[待确认: 具体说明]] 占位。
- 不要附件、不要图片、不要多个链接、不要 P.S. 促销话术、不要"limited time offer"。
- 语气：同行之间的正常商务往来，不是推销。

{_ANTI_HALLUCINATION}

这封信会由销售本人审核修改后手动发出，所以宁可留白让他补，也不要替他编。"""


def reply_system_prompt(seller: SellerProfile) -> str:
    return f"""你是下面这家中国紧固件工厂的外贸助理：

{seller.as_prompt_block()}

客户回信了。你要做两件事：
（一）分析对方的意图、紧急度、具体诉求和顾虑；
（二）起草一封回信。

分析部分用中文，回信正文用对方的语言。

回信规则：
- 直接回应对方问的问题，不要绕。
- 价格、交期、能否做某规格、能否提供某认证 —— 这些你都不知道，一律用
  [[待确认: 具体说明]] 占位，让销售填。绝对不要编数字。
- 如果对方明确表示不感兴趣或要求退订，回信就写一封简短得体的收尾，
  不要再推销，并在 recommended_action 里注明「加入不再联系名单」。
- 如果是自动回复（out of office）或明显发错人，回信留空字符串，
  并在 recommended_action 里说明。

{_ANTI_HALLUCINATION}"""


# --------------------------------------------------------------------------- #
# user 消息拼装
# --------------------------------------------------------------------------- #

def _fmt_records(records: list[dict[str, Any]], limit: int = 15) -> str:
    if not records:
        return "（无提单记录）"
    lines = []
    for r in records[:limit]:
        parts = [
            f"日期={r.get('shipment_date') or '未知'}",
            f"HS={r.get('hs_code') or '未知'}",
            f"品名={r.get('product_desc') or '未知'}",
        ]
        if r.get("quantity"):
            parts.append(f"数量={r['quantity']:.0f}{r.get('unit') or ''}")
        if r.get("value_usd"):
            parts.append(f"金额=${r['value_usd']:,.0f}")
        if r.get("supplier"):
            parts.append(f"供应商={r['supplier']}")
        if r.get("origin"):
            parts.append(f"起运={r['origin']}")
        lines.append("- " + " | ".join(parts))
    if len(records) > limit:
        lines.append(f"…（另有 {len(records) - limit} 票，已省略）")
    return "\n".join(lines)


def _fmt_company(company: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"公司名: {company.get('name')}",
            f"国家/地区: {company.get('country') or '未知'}",
            f"官网: {company.get('website') or company.get('domain') or '未知'}",
            f"已知邮箱: {company.get('contact_email') or '无'}",
            f"数据来源: {company.get('source') or '未知'}",
        ]
    )


def _fmt_site(enrichment: dict[str, Any] | None, max_chars: int = 9000) -> str:
    if not enrichment:
        return "（未抓取官网）"
    status = enrichment.get("status")
    if status != "ok":
        return f"（官网抓取未成功：{status} — {enrichment.get('error') or ''}）"
    pages = enrichment.get("pages") or []
    header = "抓取到的页面：\n" + "\n".join(
        f"- {p.get('title') or '(无标题)'} — {p.get('url')}" for p in pages
    )
    emails = enrichment.get("emails") or []
    email_line = f"\n页面上出现的邮箱：{', '.join(emails)}" if emails else "\n页面上没找到邮箱"
    text = (enrichment.get("text") or "")[:max_chars]
    return f"{header}{email_line}\n\n官网正文节选：\n{text}"


def analysis_user_prompt(
    company: dict[str, Any],
    records: list[dict[str, Any]],
    enrichment: dict[str, Any] | None,
    prescore: int,
    prescore_reasons: list[str],
) -> str:
    return f"""# 材料

## 1. 公司基本信息（来自海关数据）
{_fmt_company(company)}

## 2. 海关提单记录
{_fmt_records(records)}

## 3. 官网抓取内容
{_fmt_site(enrichment)}

## 4. 规则预打分（仅供参考，你可以推翻）
分数 {prescore}/100，依据：
{chr(10).join('- ' + r for r in prescore_reasons) or '- （无）'}

# 任务
基于以上材料输出公司画像、切入点分析和优先级判断。
如果你认为这条线索根本不值得跟进（比如是货代、是同行工厂、品类完全不符），
直接给 low 并在 reasons 里说清楚原因 —— 帮销售省时间和帮他找客户一样有价值。"""


def cold_email_user_prompt(
    company: dict[str, Any],
    analysis: dict[str, Any],
    records: list[dict[str, Any]],
    enrichment: dict[str, Any] | None,
    language: str,
) -> str:
    profile = analysis.get("profile") or {}
    angles = analysis.get("angles") or []
    angle_lines = "\n".join(
        f"{i}. {a.get('angle')}（依据：{a.get('evidence')}）"
        for i, a in enumerate(angles, 1)
    ) or "（无）"
    return f"""# 收件方
{_fmt_company(company)}

# 已有的公司画像
业务概述: {profile.get('business_summary') or '未知'}
供应链角色: {profile.get('likely_role') or 'unknown'}
主营品类: {profile.get('products_focus') or '未知'}
采购特征: {profile.get('buying_pattern') or '未知'}
建议联系岗位: {profile.get('decision_maker_guess') or '未知'}

# 可用的切入点（按优先级）
{angle_lines}

# 提单事实（可引用，但不要逐条罗列进信里）
{_fmt_records(records, limit=8)}

# 官网信息
{_fmt_site(enrichment, max_chars=3500)}

# 任务
用 {language} 写一封开发信。优先使用第 1 个切入点。
personalization_used 字段里逐条写清楚你引用了哪些事实、来自哪里，
方便销售在发送前核对真假。"""


def reply_user_prompt(
    company: dict[str, Any],
    original_draft: dict[str, Any] | None,
    reply: dict[str, Any],
    analysis: dict[str, Any] | None,
) -> str:
    sent_block = "（找不到原始去信记录）"
    if original_draft:
        sent_block = (
            f"主题: {original_draft.get('subject')}\n\n{original_draft.get('body')}"
        )
    profile_block = ""
    if analysis:
        profile = analysis.get("profile") or {}
        profile_block = (
            f"\n# 我们之前对这家公司的判断\n"
            f"{profile.get('business_summary') or '未知'}\n"
            f"采购特征: {profile.get('buying_pattern') or '未知'}\n"
        )
    return f"""# 客户
{_fmt_company(company)}
{profile_block}
# 我们发出去的信
{sent_block}

# 对方的回复
发件人: {reply.get('from_email')}
主题: {reply.get('subject')}
时间: {reply.get('received_at')}

{(reply.get('body') or '')[:6000]}

# 任务
分析这封回复，并起草回信。"""
