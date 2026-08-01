"""早上的汇报：把昨晚跑批的结果整理成一页能在 30 秒内看完的简报。

输出 Markdown（终端/邮件用）和 HTML（浏览器用）两种。
"""

from __future__ import annotations

import html
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from . import store

_PRIORITY_LABEL = {"high": "高优先级", "medium": "中优先级", "low": "低优先级"}
_PRIORITY_EMOJI = {"high": "🔥", "medium": "🟡", "low": "⚪"}


def build_brief(conn: sqlite3.Connection, run_id: int | None = None) -> dict[str, Any]:
    """汇总一次跑批的结果，返回结构化数据（Markdown/HTML 都基于它渲染）。"""
    run = store.get_run(conn, run_id) if run_id else store.latest_run(conn)
    if run is None:
        return {"run": None, "items": [], "pending_drafts": [], "replies": []}

    analyses = store.companies_for_run(conn, run["id"])
    drafts_by_analysis = {
        d["analysis_id"]: d
        for d in store.list_drafts(conn, status=None, limit=500)
        if d.get("analysis_id")
    }

    items = []
    for analysis in analyses:
        draft = drafts_by_analysis.get(analysis["id"])
        items.append(
            {
                "company_id": analysis["company_id"],
                "company_name": analysis["company_name"],
                "country": analysis["company_country"],
                "website": analysis["website"],
                "priority": analysis["priority"],
                "score": analysis["score"],
                "prescore": analysis["prescore"],
                "reasons": analysis["reasons"],
                "risks": analysis["risks"],
                "profile": analysis["profile"],
                "angles": analysis["angles"],
                "draft": draft,
            }
        )

    return {
        "run": run,
        "items": items,
        "pending_drafts": store.list_drafts(conn, status="pending", limit=200),
        "replies": store.list_replies(conn, handled=False, limit=50),
    }


def _fmt_run_window(run: dict[str, Any]) -> str:
    started = run.get("started_at") or ""
    finished = run.get("finished_at") or ""
    try:
        s = datetime.fromisoformat(started).strftime("%m-%d %H:%M")
        f = datetime.fromisoformat(finished).strftime("%H:%M") if finished else "进行中"
        return f"{s} → {f} (UTC)"
    except ValueError:
        return started


def render_markdown(brief: dict[str, Any]) -> str:
    run = brief["run"]
    if run is None:
        return "# BoltMind 早报\n\n还没有任何跑批记录。先执行 `python -m boltmind.cli run-night`。"

    stats = run.get("stats") or {}
    items = brief["items"]
    high = [i for i in items if i["priority"] == "high"]
    medium = [i for i in items if i["priority"] == "medium"]
    low = [i for i in items if i["priority"] == "low"]

    lines: list[str] = []
    lines.append("# BoltMind 早报")
    lines.append("")
    lines.append(f"跑批 #{run['id']} · {_fmt_run_window(run)} · 状态 `{run['status']}`")
    lines.append("")
    lines.append(
        f"**昨晚处理了 {stats.get('analyzed', 0)} 家公司，"
        f"其中 {len(high)} 家高优先级，生成 {stats.get('drafts_created', 0)} 封草稿待你审核。**"
    )
    lines.append("")
    lines.append(
        f"- 新线索 {stats.get('leads_seen', 0)} 条 → 新增公司 {stats.get('companies_new', 0)} 家"
        f"，新增提单 {stats.get('records_new', 0)} 条"
    )
    lines.append(
        f"- 官网抓取：成功 {stats.get('crawled_ok', 0)} / 失败 {stats.get('crawled_failed', 0)}"
    )
    lines.append(
        f"- AI 花费：${stats.get('cost_usd', 0):.2f}"
        + ("（**预算用尽提前停止**，剩余公司留到下一轮）" if stats.get("budget_stopped") else "")
    )
    if stats.get("skipped_no_contact"):
        lines.append(
            f"- ⚠️ {stats['skipped_no_contact']} 家没找到可信邮箱，草稿已生成但收件人待补"
        )
    if stats.get("errors"):
        lines.append(f"- ⚠️ {len(stats['errors'])} 条处理错误（见 run 详情）")
    lines.append("")

    if brief["replies"]:
        lines.append(f"## 📬 待处理回复（{len(brief['replies'])}）")
        lines.append("")
        for reply in brief["replies"]:
            lines.append(
                f"- **{reply.get('company_name') or reply.get('from_email')}** "
                f"· 意图 `{reply.get('intent') or '未分析'}` "
                f"· {reply.get('summary') or reply.get('subject') or ''}"
            )
        lines.append("")

    for title, group in (
        ("🔥 高优先级 —— 建议今天就发", high),
        ("🟡 中优先级 —— 有空再看", medium),
        ("⚪ 低优先级 —— 已判断不值得跟进", low),
    ):
        if not group:
            continue
        lines.append(f"## {title}（{len(group)}）")
        lines.append("")
        for item in group:
            lines.extend(_render_item_md(item, detailed=item["priority"] != "low"))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "所有草稿都停在 `pending` 状态。审核修改后由你手动点发送："
        "`streamlit run app.py`，或 `python -m boltmind.cli send --draft-id <id>`。"
    )
    return "\n".join(lines)


def _render_item_md(item: dict[str, Any], detailed: bool = True) -> list[str]:
    emoji = _PRIORITY_EMOJI.get(item["priority"], "")
    header = f"### {emoji} {item['company_name']}"
    if item.get("country"):
        header += f" · {item['country']}"
    lines = [header, ""]
    lines.append(
        f"评分 {item['score']}/100（规则预打分 {item['prescore']}）"
        + (f" · [官网]({item['website']})" if item.get("website") else "")
    )
    lines.append("")
    lines.append(f"**为什么是这个优先级**：{item.get('reasons') or '—'}")

    if not detailed:
        lines.append("")
        return lines

    profile = item.get("profile") or {}
    if profile.get("business_summary"):
        lines.append("")
        lines.append(f"**画像**：{profile['business_summary']}")
        role = profile.get("likely_role")
        if role and role != "unknown":
            lines.append(f"　角色：{role} · 采购特征：{profile.get('buying_pattern') or '—'}")

    angles = item.get("angles") or []
    if angles:
        lines.append("")
        lines.append("**切入点**：")
        for i, angle in enumerate(angles, 1):
            lines.append(f"{i}. {angle.get('angle')} — *依据：{angle.get('evidence')}*")

    if item.get("risks"):
        lines.append("")
        lines.append(f"**风险**：{item['risks']}")

    draft = item.get("draft")
    if draft:
        lines.append("")
        lines.append(
            f"**草稿 #{draft['id']}**（{draft.get('language') or '?'}）"
            f" → `{draft.get('to_email') or '⚠️ 收件人待补'}`"
        )
        lines.append("")
        lines.append(f"> **{draft.get('subject') or '(无主题)'}**")
        lines.append(">")
        for line in (draft.get("body") or "").splitlines():
            lines.append(f"> {line}")
    else:
        lines.append("")
        lines.append("_（未生成草稿）_")

    lines.append("")
    return lines


_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BoltMind 早报</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  max-width: 860px; margin: 0 auto; padding: 24px 18px 64px; line-height: 1.65; }}
h1 {{ font-size: 1.7rem; margin-bottom: .2em; }}
h2 {{ margin-top: 2em; padding-bottom: .3em; border-bottom: 1px solid rgba(128,128,128,.3); }}
h3 {{ margin-top: 1.6em; }}
.lede {{ font-size: 1.1rem; font-weight: 600; padding: 14px 16px; border-radius: 10px;
  background: rgba(255,133,27,.12); border-left: 4px solid #FF851B; }}
.meta {{ color: #888; font-size: .9rem; }}
.card {{ border: 1px solid rgba(128,128,128,.28); border-radius: 12px;
  padding: 16px 18px; margin: 14px 0; }}
.card.high {{ border-left: 5px solid #FF4136; }}
.card.medium {{ border-left: 5px solid #FF851B; }}
.card.low {{ border-left: 5px solid #aaa; opacity: .75; }}
.badge {{ display: inline-block; font-size: .78rem; padding: 2px 9px; border-radius: 999px;
  background: rgba(128,128,128,.18); margin-right: 6px; }}
.draft {{ background: rgba(128,128,128,.1); border-radius: 8px; padding: 12px 14px;
  margin-top: 12px; white-space: pre-wrap; font-size: .93rem; }}
.draft .subject {{ font-weight: 700; display: block; margin-bottom: 8px; }}
.warn {{ color: #d33; font-weight: 600; }}
ul {{ padding-left: 1.2em; }}
table {{ border-collapse: collapse; width: 100%; overflow-x: auto; display: block; }}
td, th {{ padding: 6px 10px; border-bottom: 1px solid rgba(128,128,128,.2); text-align: left; }}
</style></head><body>
{content}
</body></html>"""


def render_html(brief: dict[str, Any]) -> str:
    run = brief["run"]
    if run is None:
        return _HTML_TEMPLATE.format(
            content="<h1>BoltMind 早报</h1><p>还没有任何跑批记录。</p>"
        )

    stats = run.get("stats") or {}
    items = brief["items"]
    high = [i for i in items if i["priority"] == "high"]

    esc = html.escape
    parts = [
        "<h1>BoltMind 早报</h1>",
        f"<p class='meta'>跑批 #{run['id']} · {esc(_fmt_run_window(run))} · "
        f"状态 {esc(str(run['status']))}</p>",
        f"<p class='lede'>昨晚处理了 {stats.get('analyzed', 0)} 家公司，"
        f"其中 {len(high)} 家高优先级，生成 {stats.get('drafts_created', 0)} 封草稿待审核。</p>",
        "<ul>",
        f"<li>新线索 {stats.get('leads_seen', 0)} 条 → 新增公司 "
        f"{stats.get('companies_new', 0)} 家，新增提单 {stats.get('records_new', 0)} 条</li>",
        f"<li>官网抓取：成功 {stats.get('crawled_ok', 0)} / 失败 "
        f"{stats.get('crawled_failed', 0)}</li>",
        f"<li>AI 花费：${stats.get('cost_usd', 0):.2f}"
        + ("<span class='warn'>（预算用尽提前停止）</span>" if stats.get("budget_stopped") else "")
        + "</li>",
    ]
    if stats.get("skipped_no_contact"):
        parts.append(
            f"<li class='warn'>{stats['skipped_no_contact']} 家没找到可信邮箱，收件人待补</li>"
        )
    parts.append("</ul>")

    if brief["replies"]:
        parts.append(f"<h2>📬 待处理回复（{len(brief['replies'])}）</h2><ul>")
        for reply in brief["replies"]:
            parts.append(
                f"<li><b>{esc(str(reply.get('company_name') or reply.get('from_email') or ''))}</b>"
                f" · <span class='badge'>{esc(str(reply.get('intent') or '未分析'))}</span>"
                f"{esc(str(reply.get('summary') or reply.get('subject') or ''))}</li>"
            )
        parts.append("</ul>")

    for title, priority in (
        ("🔥 高优先级 —— 建议今天就发", "high"),
        ("🟡 中优先级 —— 有空再看", "medium"),
        ("⚪ 低优先级 —— 已判断不值得跟进", "low"),
    ):
        group = [i for i in items if i["priority"] == priority]
        if not group:
            continue
        parts.append(f"<h2>{title}（{len(group)}）</h2>")
        for item in group:
            parts.append(_render_item_html(item, esc))

    parts.append(
        "<hr><p class='meta'>所有草稿都停在 pending 状态，"
        "审核修改后由你手动点发送。</p>"
    )
    return _HTML_TEMPLATE.format(content="\n".join(parts))


def _render_item_html(item: dict[str, Any], esc) -> str:
    profile = item.get("profile") or {}
    angles = item.get("angles") or []
    draft = item.get("draft")

    parts = [f"<div class='card {item['priority']}'>"]
    title = esc(str(item["company_name"]))
    if item.get("website"):
        title = f"<a href='{esc(str(item['website']))}' rel='noreferrer'>{title}</a>"
    parts.append(f"<h3>{title}</h3>")
    parts.append(
        f"<p class='meta'><span class='badge'>{_PRIORITY_LABEL.get(item['priority'], '')}</span>"
        f"<span class='badge'>{esc(str(item.get('country') or '—'))}</span>"
        f"评分 {item['score']}/100（规则 {item['prescore']}）</p>"
    )
    parts.append(f"<p><b>为什么是这个优先级：</b>{esc(str(item.get('reasons') or '—'))}</p>")

    if profile.get("business_summary"):
        parts.append(f"<p><b>画像：</b>{esc(str(profile['business_summary']))}</p>")
    if angles:
        parts.append("<p><b>切入点：</b></p><ul>")
        for angle in angles:
            parts.append(
                f"<li>{esc(str(angle.get('angle') or ''))}"
                f"<span class='meta'> —— 依据：{esc(str(angle.get('evidence') or ''))}</span></li>"
            )
        parts.append("</ul>")
    if item.get("risks"):
        parts.append(f"<p><b>风险：</b>{esc(str(item['risks']))}</p>")

    if draft:
        recipient = draft.get("to_email") or "⚠️ 收件人待补"
        parts.append(
            f"<div class='draft'><span class='subject'>#{draft['id']} → {esc(str(recipient))}"
            f"　|　{esc(str(draft.get('subject') or '(无主题)'))}</span>"
            f"{esc(str(draft.get('body') or ''))}</div>"
        )
        if draft.get("rationale"):
            parts.append(f"<p class='meta'>{esc(str(draft['rationale']))}</p>")
    parts.append("</div>")
    return "\n".join(parts)


def write_brief(
    conn: sqlite3.Connection,
    out_dir: Path,
    run_id: int | None = None,
) -> tuple[Path, Path, str]:
    """生成 Markdown + HTML 两份简报，返回 (md 路径, html 路径, markdown 文本)。"""
    brief = build_brief(conn, run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    md_text = render_markdown(brief)
    md_path = out_dir / f"brief-{stamp}.md"
    html_path = out_dir / f"brief-{stamp}.html"
    md_path.write_text(md_text, encoding="utf-8")
    html_path.write_text(render_html(brief), encoding="utf-8")
    return md_path, html_path, md_text
