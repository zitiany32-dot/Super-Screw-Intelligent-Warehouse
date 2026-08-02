"""CustomsRadar 获客后台 —— 早上打开这个页面，30 分钟审完，亲手点发送。

    streamlit run app.py

页面本身不会自动发任何邮件：只有你点「确认发送」那一下才会真的走 SMTP，
而且要求草稿已经过审、配置里 RADAR_ALLOW_SEND 已打开。
"""

from __future__ import annotations

import streamlit as st

from customsradar import brief as brief_mod
from customsradar import inbox as inbox_mod
from customsradar import mailer, store
from customsradar.config import Config
from customsradar.db import init_db

st.set_page_config(page_title="CustomsRadar 获客后台", layout="wide", page_icon="🧲")

PRIMARY = "#032360"
ACCENT = "#FF851B"

st.markdown(
    f"""
    <style>
    .block-container {{ padding-top: 2rem; }}
    [data-testid="stSidebar"] {{ background-color: {PRIMARY}; }}
    [data-testid="stSidebar"] * {{ color: #fff; }}
    div.stButton > button[kind="primary"] {{
        background-color: {ACCENT}; color: #fff; font-weight: 700; border: none;
    }}
    .lede {{ font-size: 1.05rem; font-weight: 600; padding: 14px 16px;
        border-radius: 10px; background: rgba(255,133,27,.14);
        border-left: 4px solid {ACCENT}; margin-bottom: 1rem; }}
    .pill {{ display:inline-block; padding:2px 10px; border-radius:999px;
        font-size:.78rem; background:rgba(128,128,128,.2); margin-right:6px; }}
    .pill-high {{ background:#FF4136; color:#fff; }}
    .pill-medium {{ background:{ACCENT}; color:#fff; }}
    .pill-low {{ background:#9aa; color:#fff; }}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_config() -> Config:
    config = Config.load()
    config.ensure_dirs()
    return config


def get_conn(config: Config):
    """每次交互开一条新连接 —— SQLite 连接不能跨 Streamlit 线程复用。"""
    return init_db(config.db_path)


config = get_config()

# --------------------------------------------------------------------------- #
# 侧边栏
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown("## 🧲 CustomsRadar")
    st.caption("获客模块 · 人工扣扳机")

    conn = get_conn(config)
    try:
        pending = len(store.list_drafts(conn, status="pending", limit=500))
        approved = len(store.list_drafts(conn, status="approved", limit=500))
        unhandled = len(store.list_replies(conn, handled=False, limit=500))
        run = store.latest_run(conn)
    finally:
        conn.close()

    st.metric("待审核草稿", pending)
    st.metric("已过审待发送", approved)
    st.metric("待处理回复", unhandled)

    st.divider()
    if config.allow_send:
        st.success("发送开关：已打开")
    else:
        st.warning("发送开关：关闭\n\n只能导出 .eml 手动发。要在后台直发，把 RADAR_ALLOW_SEND=true 写进 .env。")
    if run:
        st.caption(f"最近跑批 #{run['id']} · {run['status']} · ${run.get('cost_usd') or 0:.2f}")

    st.divider()
    st.caption("跑批请用命令行 / 定时任务：")
    st.code("python -m customsradar.cli run-night", language="bash")


tab_brief, tab_drafts, tab_replies, tab_leads = st.tabs(
    ["📋 早报", "✍️ 审核草稿", "📬 回复箱", "🗂 线索库"]
)


# --------------------------------------------------------------------------- #
# 早报
# --------------------------------------------------------------------------- #
with tab_brief:
    conn = get_conn(config)
    try:
        data = brief_mod.build_brief(conn)
    finally:
        conn.close()

    run = data["run"]
    if run is None:
        st.info("还没有跑批记录。先执行 `python -m customsradar.cli run-night --demo` 试跑一次。")
    else:
        stats = run.get("stats") or {}
        high = [i for i in data["items"] if i["priority"] == "high"]
        st.markdown(
            f"<div class='lede'>昨晚处理了 {stats.get('analyzed', 0)} 家公司，"
            f"其中 {len(high)} 家高优先级，生成 {stats.get('drafts_created', 0)} 封草稿待审核。</div>",
            unsafe_allow_html=True,
        )

        cols = st.columns(5)
        cols[0].metric("新线索", stats.get("leads_seen", 0))
        cols[1].metric("新公司", stats.get("companies_new", 0))
        cols[2].metric("官网抓取成功", stats.get("crawled_ok", 0))
        cols[3].metric("生成草稿", stats.get("drafts_created", 0))
        cols[4].metric("AI 花费", f"${stats.get('cost_usd', 0):.2f}")

        if stats.get("budget_stopped"):
            st.warning("预算用尽，本轮提前停止。剩余公司会在下一轮继续。")
        if stats.get("skipped_no_contact"):
            st.warning(f"{stats['skipped_no_contact']} 家没找到可信邮箱，草稿已生成但收件人待补。")
        if stats.get("errors"):
            with st.expander(f"⚠️ {len(stats['errors'])} 条处理错误"):
                for err in stats["errors"]:
                    st.text(err)

        st.divider()
        for label, priority in (
            ("🔥 高优先级 —— 建议今天就发", "high"),
            ("🟡 中优先级 —— 有空再看", "medium"),
            ("⚪ 低优先级 —— 已判断不值得跟进", "low"),
        ):
            group = [i for i in data["items"] if i["priority"] == priority]
            if not group:
                continue
            st.subheader(f"{label}（{len(group)}）")
            for item in group:
                with st.expander(
                    f"{item['company_name']} · {item.get('country') or '—'} · "
                    f"{item['score']}/100",
                    expanded=(priority == "high"),
                ):
                    st.markdown(f"**为什么是这个优先级：** {item.get('reasons') or '—'}")
                    profile = item.get("profile") or {}
                    if profile.get("business_summary"):
                        st.markdown(f"**画像：** {profile['business_summary']}")
                        st.caption(
                            f"角色 {profile.get('likely_role') or '?'} · "
                            f"采购特征 {profile.get('buying_pattern') or '?'} · "
                            f"建议联系 {profile.get('decision_maker_guess') or '?'}"
                        )
                    assessment = item.get("assessment") or {}
                    if any(
                        assessment.get(k)
                        for k in ("strengths", "weaknesses", "opportunities", "threats")
                    ):
                        cc = st.columns(2)
                        with cc[0]:
                            if assessment.get("strengths"):
                                st.markdown("**✅ 优势**")
                                for v in assessment["strengths"]:
                                    st.markdown(f"- {v}")
                            if assessment.get("opportunities"):
                                st.markdown("**🎯 机会**")
                                for v in assessment["opportunities"]:
                                    st.markdown(f"- {v}")
                        with cc[1]:
                            if assessment.get("weaknesses"):
                                st.markdown("**⚠️ 劣势**")
                                for v in assessment["weaknesses"]:
                                    st.markdown(f"- {v}")
                            if assessment.get("threats"):
                                st.markdown("**🚩 风险**")
                                for v in assessment["threats"]:
                                    st.markdown(f"- {v}")
                        if assessment.get("fit_verdict"):
                            st.info(f"📌 {assessment['fit_verdict']}")
                    for i, angle in enumerate(item.get("angles") or [], 1):
                        st.markdown(
                            f"**切入点 {i}：** {angle.get('angle')}  \n"
                            f"<span class='pill'>依据</span>{angle.get('evidence')}",
                            unsafe_allow_html=True,
                        )
                    if item.get("emails"):
                        st.markdown("**找到的邮箱**（按可信度）：")
                        st.dataframe(
                            [
                                {
                                    "邮箱": c["email"],
                                    "来源": c.get("source"),
                                    "校验": c.get("verified"),
                                    "可信度": c.get("confidence"),
                                    "联系人": c.get("person_name") or "",
                                }
                                for c in item["emails"][:8]
                            ],
                            use_container_width=True,
                            hide_index=True,
                        )
                    if item.get("risks"):
                        st.warning(f"风险：{item['risks']}")
                    draft = item.get("draft")
                    if draft:
                        st.markdown(
                            f"**草稿 #{draft['id']}** → `{draft.get('to_email') or '⚠️ 待补'}`"
                        )
                        st.text_area(
                            f"预览 #{draft['id']}",
                            value=f"{draft.get('subject')}\n\n{draft.get('body')}",
                            height=220,
                            disabled=True,
                            key=f"brief_preview_{draft['id']}",
                        )
                    if item.get("website"):
                        st.caption(item["website"])


# --------------------------------------------------------------------------- #
# 审核草稿
# --------------------------------------------------------------------------- #
with tab_drafts:
    left, right = st.columns([1, 3])
    with left:
        status_filter = st.selectbox(
            "状态", ["pending", "approved", "sent", "rejected", "failed"], index=0
        )
        kind_filter = st.selectbox("类型", ["全部", "cold", "reply"], index=0)

    conn = get_conn(config)
    try:
        drafts = store.list_drafts(
            conn,
            status=status_filter,
            kind=None if kind_filter == "全部" else kind_filter,
            limit=200,
        )
    finally:
        conn.close()

    if not drafts:
        st.info(f"没有 {status_filter} 状态的草稿。")
    else:
        st.caption(f"共 {len(drafts)} 封。改完点「保存并通过审核」，再点「确认发送」。")

    for draft in drafts:
        priority = draft.get("priority") or "low"
        pill = f"<span class='pill pill-{priority}'>{priority}</span>"
        header = (
            f"#{draft['id']} · {draft.get('company_name') or '?'} · "
            f"{draft.get('country') or draft.get('company_country') or '—'} · "
            f"{draft.get('subject') or '(无主题)'}"
        )
        with st.expander(header, expanded=False):
            st.markdown(
                f"{pill}<span class='pill'>{draft.get('kind')}</span>"
                f"<span class='pill'>{draft.get('language') or '?'}</span>"
                f"评分 {draft.get('score') or '—'}/100",
                unsafe_allow_html=True,
            )
            if draft.get("reasons"):
                st.markdown(f"**优先级理由：** {draft['reasons']}")
            if draft.get("rationale"):
                with st.expander("起草说明 / 需人工确认的地方", expanded=False):
                    st.text(draft["rationale"])

            to_email = st.text_input(
                "收件人", value=draft.get("to_email") or "", key=f"to_{draft['id']}"
            )
            subject = st.text_input(
                "主题", value=draft.get("subject") or "", key=f"subj_{draft['id']}"
            )
            body = st.text_area(
                "正文", value=draft.get("body") or "", height=300, key=f"body_{draft['id']}"
            )

            c1, c2, c3, c4 = st.columns(4)

            if c1.button("💾 保存并通过审核", key=f"approve_{draft['id']}"):
                conn = get_conn(config)
                try:
                    mailer.approve_draft(
                        conn, draft["id"], subject=subject, body=body, to_email=to_email
                    )
                    st.success("已保存并标记为 approved")
                except mailer.SendBlocked as exc:
                    st.error(str(exc))
                finally:
                    conn.close()
                st.rerun()

            if c2.button("🚫 否掉", key=f"reject_{draft['id']}"):
                conn = get_conn(config)
                try:
                    mailer.reject_draft(conn, draft["id"], "后台人工否决")
                finally:
                    conn.close()
                st.rerun()

            if c3.button("📎 导出 .eml", key=f"eml_{draft['id']}"):
                conn = get_conn(config)
                try:
                    current = store.get_draft(conn, draft["id"])
                    current = {**current, "subject": subject, "body": body, "to_email": to_email}
                    path = mailer.export_eml(config, current, config.data_dir / "eml")
                    st.success(f"已导出：{path}")
                finally:
                    conn.close()

            send_disabled = draft["status"] != "approved" or not config.allow_send
            if c4.button(
                "🚀 确认发送",
                key=f"send_{draft['id']}",
                type="primary",
                disabled=send_disabled,
                help=(
                    "先点「保存并通过审核」"
                    if draft["status"] != "approved"
                    else ("RADAR_ALLOW_SEND 未打开" if not config.allow_send else "")
                ),
            ):
                conn = get_conn(config)
                try:
                    result = mailer.send_draft(conn, config, draft["id"], confirm=True)
                    st.success(f"已发送 → {result.to_email}")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"发送失败：{exc}")
                finally:
                    conn.close()
                st.rerun()

            if draft.get("error"):
                st.error(draft["error"])
            if draft.get("sent_at"):
                st.caption(f"已于 {draft['sent_at']} 发送 · {draft.get('message_id')}")


# --------------------------------------------------------------------------- #
# 回复箱
# --------------------------------------------------------------------------- #
with tab_replies:
    col_a, col_b = st.columns([1, 4])
    with col_a:
        show_handled = st.checkbox("包含已处理", value=False)
    with col_b:
        if st.button("📥 拉取新回复（IMAP）"):
            if not config.imap_host:
                st.error("没有配置 IMAP_HOST")
            else:
                conn = get_conn(config)
                try:
                    with st.spinner("正在收信并分析…"):
                        stats = inbox_mod.poll(conn, config)
                    st.success(
                        f"新回复 {stats.new_replies} 封，生成回信草稿 "
                        f"{stats.reply_drafts} 封，花费 ${stats.cost_usd:.2f}"
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"收信失败：{exc}")
                finally:
                    conn.close()

    conn = get_conn(config)
    try:
        replies = store.list_replies(conn, handled=None if show_handled else False)
    finally:
        conn.close()

    if not replies:
        st.info("暂无待处理回复。")
    for reply in replies:
        with st.expander(
            f"{reply.get('company_name') or reply.get('from_email')} · "
            f"{reply.get('intent') or '未分析'} · {reply.get('subject') or ''}"
        ):
            st.caption(f"{reply.get('from_email')} · {reply.get('received_at')}")
            if reply.get("summary"):
                st.markdown(f"**AI 摘要：** {reply['summary']}")
            st.text_area(
                "原文",
                value=reply.get("body") or "",
                height=200,
                disabled=True,
                key=f"reply_body_{reply['id']}",
            )
            conn = get_conn(config)
            try:
                related = [
                    d
                    for d in store.list_drafts(conn, status=None, kind="reply", limit=200)
                    if d.get("reply_id") == reply["id"]
                ]
            finally:
                conn.close()
            if related:
                st.caption(
                    "回信草稿："
                    + "、".join(f"#{d['id']}（{d['status']}）" for d in related)
                    + " —— 去「审核草稿」页处理"
                )
            if not reply.get("handled") and st.button(
                "标记为已处理", key=f"handled_{reply['id']}"
            ):
                conn = get_conn(config)
                try:
                    store.update_reply(conn, reply["id"], handled=1)
                    conn.commit()
                finally:
                    conn.close()
                st.rerun()


# --------------------------------------------------------------------------- #
# 线索库
# --------------------------------------------------------------------------- #
with tab_leads:
    conn = get_conn(config)
    try:
        companies = store.list_companies(conn, limit=500)
    finally:
        conn.close()

    if not companies:
        st.info("线索库是空的。把海关数据导出文件放进 data/inbox/ 后执行 run-night。")
    else:
        st.caption(f"共 {len(companies)} 家公司")
        st.dataframe(
            [
                {
                    "ID": c["id"],
                    "公司": c["name"],
                    "国家": c.get("country"),
                    "官网": c.get("website"),
                    "邮箱": c.get("contact_email"),
                    "状态": c["status"],
                    "最近更新": (c.get("last_seen") or "")[:16],
                }
                for c in companies
            ],
            use_container_width=True,
            hide_index=True,
        )
