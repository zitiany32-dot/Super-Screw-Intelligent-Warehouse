"""命令行入口：`python -m boltmind.cli <子命令>`。

夜间跑批用 run-night，早上看 brief，审核发送用 approve / send。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import brief as brief_mod
from . import inbox, mailer, store
from .config import Config
from .db import session


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #

def cmd_init(args: argparse.Namespace, config: Config) -> int:
    config.ensure_dirs()
    (config.data_dir / "inbox").mkdir(parents=True, exist_ok=True)
    (config.data_dir / "briefs").mkdir(parents=True, exist_ok=True)
    (config.data_dir / "eml").mkdir(parents=True, exist_ok=True)
    with session(config.db_path):
        pass
    print(f"✅ 数据库已就绪: {config.db_path}")
    print(f"   把海关数据导出文件（.csv/.xlsx）放进: {config.data_dir / 'inbox'}")
    return 0


def cmd_import(args: argparse.Namespace, config: Config) -> int:
    from .pipeline import ingest
    from .sources.csv_source import CsvCustomsSource
    from .sources.demo import DemoCustomsSource

    if args.demo:
        source = DemoCustomsSource()
    else:
        if not args.path:
            print("请用 --path 指定文件或目录，或加 --demo 导入演示数据")
            return 1
        mapping = {}
        for pair in args.map or []:
            if "=" not in pair:
                print(f"--map 格式应为 字段名=列名，收到: {pair}")
                return 1
            key, value = pair.split("=", 1)
            mapping[key.strip()] = value.strip()
        source = CsvCustomsSource(Path(args.path), mapping=mapping or None)

    with session(config.db_path) as conn:
        stats = ingest(conn, source.fetch())
    print(
        f"读入 {stats.leads_seen} 条线索 → 新公司 {stats.companies_new} 家，"
        f"新提单 {stats.records_new} 条"
    )
    for err in stats.errors[:5]:
        print(f"  ⚠️ {err}")
    return 0


def cmd_run_night(args: argparse.Namespace, config: Config) -> int:
    from .pipeline import default_sources, run_night

    if args.limit:
        config.max_companies_per_night = args.limit
    if args.budget is not None:
        config.nightly_budget_usd = args.budget

    sources = default_sources(config, use_demo=args.demo)
    if not sources:
        print("没有可用数据源。把导出文件放进 data/inbox/，或加 --demo 跑演示数据。")
        return 1

    with session(config.db_path) as conn:
        run_id, stats = run_night(conn, config, sources)
        md_path, html_path, _ = brief_mod.write_brief(
            conn, config.data_dir / "briefs", run_id
        )

    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    print(f"\n📄 早报: {md_path}\n🌐 网页版: {html_path}")
    print("所有草稿状态为 pending，等你审核后手动发送。")
    return 0


def cmd_brief(args: argparse.Namespace, config: Config) -> int:
    with session(config.db_path) as conn:
        md_path, html_path, text = brief_mod.write_brief(
            conn, config.data_dir / "briefs", args.run_id
        )
    if args.print:
        print(text)
    else:
        print(f"📄 {md_path}\n🌐 {html_path}")
    return 0


def cmd_drafts(args: argparse.Namespace, config: Config) -> int:
    with session(config.db_path) as conn:
        drafts = store.list_drafts(conn, status=args.status, kind=args.kind)
    if not drafts:
        print(f"没有 status={args.status} 的草稿。")
        return 0
    for draft in drafts:
        flag = "⚠️ 无收件人" if not draft.get("to_email") else draft["to_email"]
        print(
            f"#{draft['id']:<4} [{draft.get('priority') or '-':<6}] "
            f"{draft.get('company_name') or '?':<38.38} → {flag:<32.32} "
            f"{draft.get('subject') or ''}"
        )
    return 0


def cmd_show(args: argparse.Namespace, config: Config) -> int:
    with session(config.db_path) as conn:
        draft = store.get_draft(conn, args.draft_id)
        if draft is None:
            print(f"草稿 #{args.draft_id} 不存在")
            return 1
        company = store.get_company(conn, draft["company_id"])
        analysis = (
            store.get_analysis(conn, draft["analysis_id"])
            if draft.get("analysis_id")
            else None
        )

    print(f"=== 草稿 #{draft['id']} ({draft['status']}) ===")
    print(f"公司: {company['name'] if company else '?'} · {company.get('country') if company else ''}")
    print(f"收件人: {draft.get('to_email') or '⚠️ 待补'}")
    print(f"语言: {draft.get('language')}")
    if analysis:
        print(f"优先级: {analysis['priority']} · 评分 {analysis['score']}/100")
        print(f"理由: {analysis.get('reasons')}")
        if analysis.get("risks"):
            print(f"风险: {analysis['risks']}")
    print(f"\n主题: {draft.get('subject')}\n")
    print(draft.get("body") or "")
    if draft.get("rationale"):
        print(f"\n--- 起草说明 ---\n{draft['rationale']}")
    return 0


def cmd_approve(args: argparse.Namespace, config: Config) -> int:
    with session(config.db_path) as conn:
        try:
            draft = mailer.approve_draft(conn, args.draft_id, to_email=args.to)
        except mailer.SendBlocked as exc:
            print(f"❌ {exc}")
            return 1
    print(f"✅ 草稿 #{draft['id']} 已标记为 approved。发送: cli.py send --draft-id {draft['id']} --confirm")
    return 0


def cmd_reject(args: argparse.Namespace, config: Config) -> int:
    with session(config.db_path) as conn:
        mailer.reject_draft(conn, args.draft_id, args.reason or "")
    print(f"草稿 #{args.draft_id} 已标记为 rejected")
    return 0


def cmd_send(args: argparse.Namespace, config: Config) -> int:
    with session(config.db_path) as conn:
        draft = store.get_draft(conn, args.draft_id)
        if draft is None:
            print(f"草稿 #{args.draft_id} 不存在")
            return 1
        problems = mailer.preflight(config, draft)
        if problems:
            print("❌ 发送被拦下：")
            for problem in problems:
                print(f"   - {problem}")
            return 1
        if not args.confirm:
            print("检查通过。确认发送请加 --confirm")
            return 1
        try:
            result = mailer.send_draft(conn, config, args.draft_id, confirm=True)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 发送失败: {exc}")
            return 1
    print(f"✅ 已发送 → {result.to_email}（Message-ID: {result.message_id}）")
    return 0


def cmd_export_eml(args: argparse.Namespace, config: Config) -> int:
    with session(config.db_path) as conn:
        draft = store.get_draft(conn, args.draft_id)
        if draft is None:
            print(f"草稿 #{args.draft_id} 不存在")
            return 1
        if not draft.get("to_email"):
            print("⚠️ 该草稿没有收件人，导出的 .eml 需要你手动填 To")
        path = mailer.export_eml(config, draft, config.data_dir / "eml")
    print(f"✅ 已导出: {path}（可直接拖进邮件客户端发送）")
    return 0


def cmd_poll_replies(args: argparse.Namespace, config: Config) -> int:
    with session(config.db_path) as conn:
        stats = inbox.poll(conn, config, since_days=args.days, limit=args.limit)
    print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    if stats.reply_drafts:
        print(f"\n生成了 {stats.reply_drafts} 封回信草稿，同样等你审核。")
    return 0


def cmd_stats(args: argparse.Namespace, config: Config) -> int:
    with session(config.db_path) as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("companies", "customs_records", "analyses", "drafts", "replies")
        }
        by_status = conn.execute(
            "SELECT status, COUNT(*) FROM drafts GROUP BY status"
        ).fetchall()
        run = store.latest_run(conn)

    print("=== 数据总量 ===")
    for name, count in counts.items():
        print(f"  {name:<18} {count}")
    print("\n=== 草稿状态 ===")
    for status, count in by_status:
        print(f"  {status:<18} {count}")
    if run:
        print(f"\n=== 最近一次跑批 #{run['id']} ({run['status']}) ===")
        print(json.dumps(run["stats"], ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boltmind",
        description="BoltMind 获客模块：海关数据 → 调研 → AI 起草 → 人工审核 → 手动发送",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打开调试日志")
    parser.add_argument("--env", type=Path, default=None, help="指定 .env 文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库和目录").set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="导入海关数据文件")
    p.add_argument("--path", help="CSV/Excel 文件或目录")
    p.add_argument("--map", action="append", help="列名映射，如 company_name=买方名称")
    p.add_argument("--demo", action="store_true", help="导入演示数据（虚构）")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("run-night", help="跑一次完整的夜间流水线")
    p.add_argument("--limit", type=int, help="本轮最多处理几家公司")
    p.add_argument("--budget", type=float, help="本轮 AI 花费上限（美元）")
    p.add_argument("--demo", action="store_true", help="没有真实数据时用演示数据")
    p.set_defaults(func=cmd_run_night)

    p = sub.add_parser("brief", help="生成早报")
    p.add_argument("--run-id", type=int, help="指定跑批 ID，默认最近一次")
    p.add_argument("--print", action="store_true", help="直接打印到终端")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("drafts", help="列出草稿")
    p.add_argument("--status", default="pending", help="pending/approved/sent/rejected")
    p.add_argument("--kind", help="cold 或 reply")
    p.set_defaults(func=cmd_drafts)

    p = sub.add_parser("show", help="查看某封草稿全文")
    p.add_argument("draft_id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", help="人工审核通过")
    p.add_argument("draft_id", type=int)
    p.add_argument("--to", help="顺便修正收件人")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("reject", help="否掉一封草稿")
    p.add_argument("draft_id", type=int)
    p.add_argument("--reason")
    p.set_defaults(func=cmd_reject)

    p = sub.add_parser("send", help="发送已审核通过的草稿")
    p.add_argument("--draft-id", type=int, required=True)
    p.add_argument("--confirm", action="store_true", help="真正发出去")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("export-eml", help="导出 .eml 手动发送")
    p.add_argument("--draft-id", type=int, required=True)
    p.set_defaults(func=cmd_export_eml)

    p = sub.add_parser("poll-replies", help="拉取回复并起草回信")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_poll_replies)

    sub.add_parser("stats", help="看数据总量和最近跑批").set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    config = Config.load(args.env)
    config.ensure_dirs()
    return int(args.func(args, config) or 0)


if __name__ == "__main__":
    sys.exit(main())
