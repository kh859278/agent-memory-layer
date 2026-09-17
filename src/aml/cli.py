"""命令行入口：aml <子命令>。

CLI 只做编排，逻辑都在各模块里，方便测试与二次开发。
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from . import config as config_mod
from . import doctor as doctor_mod
from . import ingest as ingest_mod
from .retrieval import Retriever


def _cfg(args):
    overrides = {}
    for key, value in (("aml_home", getattr(args, "home", None)),
                       ("memory_api", getattr(args, "api", None)),
                       ("db_path", getattr(args, "db", None))):
        if value:
            overrides[key] = value
    return config_mod.load(overrides)


# --------------------------------------------------------------------- 基础

def cmd_init(args):
    cfg = _cfg(args)
    created = config_mod.init_home(cfg, force=args.force)
    print("已创建数据目录：")
    for key, path in created.items():
        print(f"  {key:16} {path}")
    if not args.no_probe:
        print("\n检测本机能看到的 agent 会话：")
        from .adapters import build
        for adapter in build(cfg):
            print(f"  {adapter.name:14} {len(adapter.discover())} 个会话文件")
    print("\n下一步：")
    print("  1) 起记忆服务（mcp-memory-service），或设置 memory_api 指向已有实例")
    print("  2) aml doctor      # 体检")
    print("  3) aml sync        # 采集入库（首次想从零开始：先 aml watch --seed 记基线）")
    return 0


def cmd_doctor(args):
    cfg = _cfg(args)
    checks = doctor_mod.run(cfg)
    print(doctor_mod.render(checks))
    if args.json:
        print(json.dumps([{"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                          for c in checks], ensure_ascii=False, indent=2))
    return 1 if any(c.status == doctor_mod.BAD for c in checks) else 0


def cmd_sync(args):
    cfg = _cfg(args)
    if not args.dry_run:
        from .http import MemoryAPIError, MemoryClient
        try:
            MemoryClient(cfg.api).health()
        except MemoryAPIError as e:
            print(f"记忆服务不可达：{e}", file=sys.stderr)
            print("先启动服务，或用 --dry-run 只看会采到什么。", file=sys.stderr)
            return 2

    def progress(i, total, ok, err, speed):
        print(f"   {i}/{total}  成功 {ok} 失败 {err}  {speed:.1f} 条/秒", flush=True)

    summary = ingest_mod.sync(cfg, since=args.since, dry_run=args.dry_run, limit=args.limit,
                              do_backfill=not args.no_backfill,
                              progress=None if args.quiet else progress)
    if args.dry_run:
        print(f"会写入 {summary['collected']} 条（未写库）：")
        for agent, n in sorted((summary.get("by_agent") or {}).items(), key=lambda x: -x[1]):
            print(f"   {agent:24} {n:6}")
    else:
        w = summary.get("write", {})
        print(f"采集 {summary['collected']} 条 → 新增 {w.get('ok')} / 去重 {w.get('dup')} / "
              f"失败 {w.get('error')}，耗时 {w.get('seconds')}s")
        if summary.get("backfill"):
            print(f"时间回填：{summary['backfill']['fixed']} / {summary['backfill']['rows']} 条"
                  f"（把 created_at 还原成原始时间）")
    return 0


def cmd_search(args):
    cfg = _cfg(args)
    result = Retriever(cfg).search(args.query, phase=args.phase, project=args.project,
                                   tag=args.tag, n=args.n, allow_repeat=args.allow_repeat,
                                   include_procedure=args.include_procedure)
    print(result.render())
    if args.explain:
        print("\n诊断：" + json.dumps(result.diag, ensure_ascii=False))
    return 0 if not result.empty else 1


def cmd_index(args):
    cfg = _cfg(args)
    if args.action == "rebuild":
        from .kb import build_index
        info = build_index(cfg)
        print(f"索引已重建：{info['path']}")
        print(f"  记忆 {info['memories']} 条 ｜ 沉淀 {info['knowledge']} 条 / {info['domains']} 领域 ｜ "
              f"知识库目录 {info['dirs']} 个")
        return 0
    print(json.dumps(Retriever(cfg).diagnostics(), ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------- 采集

def cmd_watch(args):
    cfg = _cfg(args)
    from . import distill, watch

    sink = None
    if not args.no_distill:
        def sink(item):  # noqa: F811 - 注入给 watch 的回调
            distill.Queue(cfg).enqueue(item["session_id"], item.get("workspace", ""),
                                       item.get("agent"), min_tasks=item.get("min_tasks", 0),
                                       reason=item.get("reason", ""))

    return watch.run(cfg, once=args.once, interval=args.interval, stable=args.stable,
                     do_seed=args.seed, dry_run=args.dry_run, distill_sink=sink)


def cmd_distill(args):
    cfg = _cfg(args)
    from . import distill
    if args.list:
        queue = distill.Queue(cfg)
        print(f"待蒸 {len(queue.data['pending'])} 个，已完成 {len(queue.data['done'])} 个")
        for item in queue.data["pending"]:
            print(f"  {item.get('short')}  {item.get('agent') or 'dsh'}  "
                  f"{item.get('workspace') or '-'}  {item.get('reason') or ''}")
        return 0
    if args.rebuild_md:
        print(f"沉淀 markdown 已重建：{distill.rebuild_markdown(cfg)}")
        return 0
    result = distill.drain(cfg, session_id=args.session)
    print(f"蒸馏完成：处理 {result['done']} 个会话，写入 {result['entries']} 条知识，"
          f"跳过 {result['skipped']}，失败 {result['failed']}")
    return 0


def cmd_ingest_kb(args):
    cfg = _cfg(args)
    from .kb import ingest_docs

    def progress(done, total, ok, dup, err):
        print(f"   {done}/{total} 块  新增 {ok} 去重 {dup} 失败 {err}", flush=True)

    dirs = [d.strip() for d in args.dirs.split(",") if d.strip()] if args.dirs else None
    exts = [e.strip() for e in args.exts.split(",") if e.strip()] if args.exts else None
    info = ingest_docs(cfg, dirs=dirs, exts=exts, since=args.since, dry_run=args.dry_run,
                       progress=None if args.quiet else progress)
    if info.get("dry_run"):
        print(f"会灌 {info['files']} 个文件 / {info['chunks']} 块（未写库）")
    else:
        print(f"灌库完成：{info['files']} 个文件 / {info['chunks']} 块 → "
              f"新增 {info['ok']} 去重 {info['dup']} 失败 {info['error']}")
    return 0


# --------------------------------------------------------------------- 维护

def cmd_backup(args):
    cfg = _cfg(args)
    from . import maintenance
    if args.list:
        rows = maintenance.list_backups(cfg)
        if not rows:
            print("还没有备份")
        for r in rows:
            print(f"{r['name']:44} {r['size_mb']:7.1f}M  {r['mtime']}")
        return 0
    info = maintenance.backup(cfg, force=args.force, keep=args.keep)
    if info["skipped"]:
        print(f"今天已经备份过，跳过（要再备一份加 --force）。目录：{info['dir']}")
    else:
        print(f"已备份 → {info['created']}（{info['size_mb']} MB）")
    if info["state_files"]:
        print(f"  同时备份状态文件：{', '.join(info['state_files'])}")
    for name in info["pruned"]:
        print(f"  清理过期备份 {name}")
    print(f"  现有备份 {info['kept']} 份")
    return 0


def cmd_restore(args):
    cfg = _cfg(args)
    from . import maintenance
    try:
        plan = maintenance.restore(cfg, name=args.name, target=args.to, confirm=args.yes)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"无法恢复：{e}", file=sys.stderr)
        return 2
    print(f"备份：{plan['backup']['name']}（{plan['backup']['size_mb']} MB，{plan['backup']['mtime']}）")
    print(f"完整性：{plan['verify']['integrity']}，{plan['verify']['memories']} 条记忆")
    print(f"目标：{plan['target']}")
    if not plan["applied"]:
        print(plan.get("hint", ""))
        return 0
    if plan.get("safety_copy"):
        print(f"恢复前的现有库已另存：{plan['safety_copy']}")
    print(f"✅ 已恢复。恢复后校验：{plan['after']['integrity']}，{plan['after']['memories']} 条记忆")
    return 0


def cmd_export(args):
    cfg = _cfg(args)
    from . import maintenance
    info = maintenance.export(cfg, args.out, fmt=args.fmt, limit=args.limit)
    print(f"已导出 {info['count']} 条 → {info['path']}（{info['fmt']}）")
    return 0


def cmd_review(args):
    cfg = _cfg(args)
    from . import feedback, maintenance
    if getattr(args, "verify", None):
        info = feedback.verify(cfg, args.verify, days=args.days)
        print(json.dumps(info, ensure_ascii=False))
        return 0 if info.get("ok") else 1
    if args.postpone:
        info = maintenance.postpone(cfg, args.postpone, days=args.days)
        print(json.dumps(info, ensure_ascii=False))
        return 0 if info.get("ok") else 1
    rows = maintenance.review_due(cfg, within_days=args.within)
    if not rows:
        print("没有到期的知识条目")
        return 0
    overdue = [r for r in rows if r["overdue"]]
    print(f"共 {len(rows)} 条需要复核（其中已过期 {len(overdue)} 条）：")
    for r in rows:
        flag = "已过期" if r["overdue"] else f"{r['days']} 天后"
        print(f"  [{flag:8}] {r['review_after']}  {r['domain']:16} {r['title'][:40]}")
    print("\n复核无误就用：aml review --postpone <hash> --days 180")
    return 0


def cmd_denoise(args):
    cfg = _cfg(args)
    from . import maintenance
    info = maintenance.denoise(cfg, apply=args.apply)
    print(f"噪声候选 {info['candidates']} 条"
          + (f"，已软删除 {info['deleted']} 条" if info["applied"] else "（预演，未删除）"))
    for row in info["sample"]:
        print(f"  {row['content'][:60]}")
    if info.get("hint"):
        print(info["hint"])
    return 0


# --------------------------------------------------------------------- 解析

def cmd_feedback(args):
    """给一条记忆打点：worked / failed / used（让"被召回"与"有用"分开计分）。"""
    cfg = _cfg(args)
    from . import feedback
    try:
        info = feedback.record(cfg, args.hash, args.outcome, note=args.note or "")
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not info.get("ok"):
        print(f"失败：{info.get('error')}", file=sys.stderr)
        return 1
    print(f"已记录：{info['outcome']}（usage {info['meta']['usage_count']}，"
          f"可靠度 {feedback.reliability(info['meta'])}）")
    return 0


def cmd_migrate(args):
    """存量迁移：给已灌进库的"程序性内容"补打 kind:procedure。"""
    cfg = _cfg(args)
    from . import migrate
    if args.action == "rollback":
        if not args.file:
            print("需要 --file <快照路径>", file=sys.stderr)
            return 2
        info = migrate.rollback(cfg, args.file)
        print(f"已回滚：恢复 {info['restored']} 条，删除 {info['removed']} 条带标签版")
        return 0

    if not args.apply:
        info = migrate.plan(cfg, limit=args.limit)
        print(f"程序性目录 {info['dirs']}：共 {info['total']} 条，已带 kind:procedure {info['tagged']} 条，"
              f"待迁移 {info['pending']} 条")
        print("（预览模式，未改动任何数据。说明：检索层已经按目录兜住了这些内容，"
              "迁移的意义是让数据本身带上标签；确认后加 --apply）")
        return 0

    def progress(done, total, ok, failed, speed):
        print(f"   {done}/{total}  成功 {ok} 失败 {failed}  {speed:.1f} 条/秒", flush=True)

    info = migrate.apply(cfg, limit=args.limit, progress=progress)
    print(f"迁移完成：{info['migrated']} 条成功，{info['failed']} 条失败，用时 {info.get('seconds')}s")
    if info.get("snapshot"):
        print(f"快照：{info['snapshot']}（回滚：aml migrate rollback --file <该文件>）")
        print("建议接着重建可读副本：aml distill --rebuild-md")
    return 0


def cmd_mcp(args):
    """把记忆层以 MCP server 形式暴露（stdio JSON-RPC）。"""
    from .mcp_server import serve
    return serve(_cfg(args))


def cmd_dedup(args):
    cfg = _cfg(args)
    from . import dedup
    if args.rollback:
        info = dedup.rollback(cfg, args.rollback)
        print(f"已回滚：恢复 {info['restored']} 条原知识，删除 {info['removed']} 条合并条目")
        return 0
    result = dedup.run(cfg, apply=args.apply, sim=args.sim, title_sim=args.title_sim,
                       show=args.show, limit=args.limit)
    if args.apply and result.get("merged"):
        print(f"完成：合并 {result['merged']} 簇，失败 {result['failed']} 簇；"
              f"快照 {result.get('backup')}")
    elif not args.apply:
        print("（预览模式，未改动数据）")
    return 0


def cmd_backfill_embeddings(args):
    cfg = _cfg(args)
    from . import embeddings

    if args.prune_orphans:
        info = embeddings.prune_orphans(cfg, apply=args.apply)
        if info.get("error"):
            print(f"无法检查：{info['error']}", file=sys.stderr)
            return 2
        print(f"孤儿记录 {info['orphans']} 条" + (f"，已删除 {info['deleted']} 条"
                                              if info["applied"] else "（预览，未删除）"))
        if not args.apply:
            return 0

    def progress(done, total, ok, failed, speed):
        print(f"   {done}/{total}  成功 {ok} 失败 {failed}  {speed:.1f} 条/秒", flush=True)

    info = embeddings.backfill(cfg, apply=args.apply, limit=args.limit, progress=progress)
    if info.get("error"):
        print(f"无法检查：{info['error']}", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aml",
                                description="跨 agent 记忆层：采集 / 蒸馏 / 分阶段检索 / 人面镜像")
    p.add_argument("--version", action="version", version=f"agent-memory-layer {__version__}")
    p.add_argument("--home", help="数据主目录（覆盖 AML_HOME）")
    p.add_argument("--api", help="记忆服务地址（覆盖 memory_api）")
    p.add_argument("--db", help="记忆服务 SQLite 路径（覆盖 db_path）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="创建数据目录骨架与 config.yaml")
    sp.add_argument("--force", action="store_true", help="覆盖已存在的 config.yaml")
    sp.add_argument("--no-probe", action="store_true", help="跳过 agent 会话探测")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("doctor", help="体检：服务 / 索引新鲜度 / 向量覆盖 / 适配器 / 检索自测")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("sync", help="采集各 agent 会话并入库（含时间回填）")
    sp.add_argument("--dry-run", action="store_true", help="只看会采集到什么，不写库")
    sp.add_argument("--since", help="只处理该日期之后的会话（YYYY-MM-DD）")
    sp.add_argument("--limit", type=int, default=0, help="最多写多少条（0=不限）")
    sp.add_argument("--no-backfill", action="store_true", help="跳过 created_at 时间回填")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("watch", help="常驻监听：归档即时入库 / 会话静默后增量入库")
    sp.add_argument("--once", action="store_true", help="跑一轮就退出")
    sp.add_argument("--seed", action="store_true", help="只记基线不导入（首次启用）")
    sp.add_argument("--dry-run", action="store_true", help="只看会做什么，不写库也不落状态")
    sp.add_argument("--interval", type=int, default=60, help="轮询间隔秒（默认 60）")
    sp.add_argument("--stable", type=int, default=120, help="文件静默多久算关闭（默认 120s）")
    sp.add_argument("--no-distill", action="store_true", help="不往蒸馏队列塞东西")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("distill", help="把会话蒸馏成跨项目知识（LLM）")
    sp.add_argument("--list", action="store_true", help="看队列")
    sp.add_argument("--session", help="只蒸指定会话（短 id）")
    sp.add_argument("--rebuild-md", action="store_true", help="从记忆层重建 沉淀/*.md")
    sp.set_defaults(func=cmd_distill)

    sp = sub.add_parser("search", help="分阶段检索（P0–P6，级联回退 + 未命中解释）")
    sp.add_argument("query")
    sp.add_argument("--phase", default="P2", help="P0–P6，决定条数与字数预算（默认 P2）")
    sp.add_argument("--project", help="优先本项目历史")
    sp.add_argument("--tag", help="收窄标签，如 domain:env-windows")
    sp.add_argument("-n", type=int, help="覆盖该阶段的条数上限")
    sp.add_argument("--allow-repeat", action="store_true", help="忽略同查询冷却")
    sp.add_argument("--include-procedure", action="store_true",
                    help="把「程序性内容」（技能正文）也纳入检索；默认跳过——它们只该显式加载")
    sp.add_argument("--explain", action="store_true", help="额外打印诊断信息")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("ingest-kb", help="把知识库文档分块灌进记忆层（增量）")
    sp.add_argument("--dirs", help="只灌这些子目录（逗号分隔）")
    sp.add_argument("--exts", help="只灌这些扩展名（逗号分隔，如 .md）")
    sp.add_argument("--since", help="只灌 mtime 晚于该时刻的文件")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_ingest_kb)

    sp = sub.add_parser("index", help="知识库索引：show / rebuild")
    sp.add_argument("action", choices=["show", "rebuild"], nargs="?", default="show")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("backup", help="备份数据库与状态文件（在线备份，按天保留）")
    sp.add_argument("--force", action="store_true", help="当天已备份也再备一份")
    sp.add_argument("--keep", type=int, default=7, help="保留份数（默认 7）")
    sp.add_argument("--list", action="store_true", help="列出已有备份")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("restore", help="从备份恢复（默认先预检；加 --yes 才真写）")
    sp.add_argument("name", nargs="?", help="备份文件名（默认用最新一份）")
    sp.add_argument("--to", help="恢复到该路径（默认覆盖配置里的 db_path）")
    sp.add_argument("--yes", action="store_true", help="确认执行（会先自动备份现有库）")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("export", help="导出成人可读的 markdown / 原始 JSON")
    sp.add_argument("out", help="输出文件路径")
    sp.add_argument("--fmt", choices=["md", "json"], default="md")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("review", help="知识复核：列到期 / 顺延 / 标记已验证")
    sp.add_argument("--within", type=int, default=0, help="也列出 N 天内到期的")
    sp.add_argument("--postpone", help="把该 hash 的复核期顺延（需配 --days）")
    sp.add_argument("--verify", help="人工确认该 hash 仍然成立：写 last_verified_at 并顺延复核期")
    sp.add_argument("--days", type=int, default=180)
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("feedback", help="记忆质量反馈：worked / failed / used（影响同档位排序）")
    sp.add_argument("--hash", required=True, help="记忆的 content_hash")
    sp.add_argument("--outcome", required=True, choices=["worked", "failed", "used"])
    sp.add_argument("--note", help="可选备注（存在 metadata.last_note）")
    sp.set_defaults(func=cmd_feedback)

    sp = sub.add_parser("denoise", help="降噪：找出界面回显/纯确认语（默认只预览）")
    sp.add_argument("--apply", action="store_true", help="执行软删除（deleted_at，可回滚）")
    sp.set_defaults(func=cmd_denoise)

    # ---- 技能治理（patrol）----
    from . import patrol_cli

    pp = sub.add_parser("patrol", help="技能治理：入库 / 上游版本监控与自动更新 / 结尾播报")
    psub = pp.add_subparsers(dest="patrol_cmd", required=True)

    sp = psub.add_parser("run", help="定时任务调这个：纳管→更新→镜像入库→包版本→播报")
    sp.add_argument("--no-adopt", action="store_true", help="跳过纳管（省流量）")
    sp.add_argument("--no-sync", action="store_true", help="更新后不同步知识库")
    sp.add_argument("--no-notify", action="store_true", help="不写播报队列")
    sp.add_argument("--deep", action="store_true", help="拿不到 sha 也下快照比内容（慢）")
    sp.set_defaults(func=lambda args: patrol_cli.cmd_patrol_run(_cfg(args), args))

    sp = psub.add_parser("sync", help="技能入库：镜像进知识库 + 重建技能清单")
    sp.add_argument("--ingest", action="store_true", help="同时增量灌进向量库")
    sp.add_argument("--force", action="store_true", help="忽略 60 秒内重复灌库的保护")
    sp.add_argument("--no-delete", action="store_true", help="只增改，不删知识库里的多余文件")
    sp.add_argument("--dry-run", action="store_true", help="只报告差异")
    sp.set_defaults(func=lambda args: patrol_cli.cmd_patrol_sync(_cfg(args), args))

    sp = psub.add_parser("adopt", help="给没有元数据的技能补上游来源（目录名命中 >=3 个才认仓库）")
    sp.add_argument("--apply", choices=["none", "no-local-only", "all"], default="none",
                    help="纳管时是否用上游覆盖本地（默认只暂存）")
    sp.add_argument("--repos", help="逗号分隔，覆盖候选仓库")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=lambda args: patrol_cli.cmd_patrol_adopt(_cfg(args), args))

    sp = psub.add_parser("check", help="只看有没有上游更新（不动任何文件）")
    sp.add_argument("--deep", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=lambda args: patrol_cli.cmd_patrol_check(_cfg(args), args))

    sp = psub.add_parser("update", help="检查并自动更新（有本地改动/补丁的只暂存）")
    sp.add_argument("--deep", action="store_true")
    sp.add_argument("--no-sync", action="store_true")
    sp.add_argument("--no-notify", action="store_true")
    sp.set_defaults(func=lambda args: patrol_cli.cmd_patrol_update(_cfg(args), args))

    sp = psub.add_parser("diff", help="看上游/暂存版改了什么（正文 diff + 新增能力信号）")
    sp.add_argument("name", nargs="?", help="只审某个技能（默认审全部待审的）")
    sp.add_argument("--fetch", action="store_true",
                    help="没有暂存版时去上游取快照再比（联网；默认只看已暂存的）")
    sp.add_argument("--max-lines", type=int, default=40, help="diff 片段行数上限")
    sp.add_argument("--fail-on-risk", action="store_true",
                    help="出现新增高风险能力信号时退出码 2（可作门禁）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=lambda args: patrol_cli.cmd_patrol_diff(_cfg(args), args))

    sp = psub.add_parser("accept", help="采纳暂存的上游版本（覆盖本地，先备份）")
    sp.add_argument("name", nargs="?", help="技能名")
    sp.add_argument("--all", action="store_true", help="全部采纳")
    sp.set_defaults(func=lambda args: patrol_cli.cmd_patrol_accept(_cfg(args), args))

    sp = psub.add_parser("packages", help="包版本监控（只监控不升级）")
    sp.add_argument("--dry-run", action="store_true", help="只打印，不写报告与播报")
    sp.add_argument("--no-notify", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=lambda args: patrol_cli.cmd_patrol_packages(_cfg(args), args))

    sp = psub.add_parser("notify", help="播报队列：--brief 取一句 ≤100 字 / --ack 标记已播报")
    sp.add_argument("--brief", action="store_true", help="打印未播报的一句话（无内容则空输出）")
    sp.add_argument("--ack", action="store_true", help="标记已播报")
    sp.add_argument("--all", action="store_true", help="配合 --ack：全部标记")
    sp.add_argument("--id", help="配合 --ack：指定 id")
    sp.add_argument("--limit", type=int, help="brief 字数上限")
    sp.set_defaults(func=lambda args: patrol_cli.cmd_patrol_notify(_cfg(args), args))

    sp = sub.add_parser("mcp", help="起 MCP server（stdio JSON-RPC），把记忆层暴露给任何 MCP 客户端")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("migrate", help="存量迁移（给已灌进库的程序性内容补打 kind:procedure，可回滚）")
    sp.add_argument("action", choices=["procedure", "rollback"], nargs="?", default="procedure")
    sp.add_argument("--apply", action="store_true", help="真的迁移（默认只预览）")
    sp.add_argument("--limit", type=int, default=0, help="只处理前 N 条（调试用）")
    sp.add_argument("--file", help="rollback 用：快照文件路径")
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("dedup", help="近义知识合并（默认只预览；--apply 才真合并，可回滚）")
    sp.add_argument("--apply", action="store_true", help="执行合并（先备份整簇，可 --rollback）")
    sp.add_argument("--sim", type=float, default=0.16, help="正文二元组相似度阈值（默认 0.16）")
    sp.add_argument("--title-sim", type=float, default=0.40, help="标题相似度阈值（默认 0.40）")
    sp.add_argument("--show", type=int, default=12, help="预览几个簇")
    sp.add_argument("--limit", type=int, default=0, help="只处理前 N 条知识（调试用）")
    sp.add_argument("--rollback", help="回滚某个快照文件")
    sp.set_defaults(func=cmd_dedup)

    sp = sub.add_parser("backfill-embeddings",
                        help="补齐缺向量的记录（缺向量 = 语义检索永远搜不到）")
    sp.add_argument("--apply", action="store_true", help="真的重存（默认只预览）")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--prune-orphans", action="store_true",
                    help="先删掉「没有向量、但已有同内容带向量副本」的孤儿记录")
    sp.set_defaults(func=cmd_backfill_embeddings)
    return p


def main(argv=None) -> int:
    from .text import ensure_utf8_stdio
    ensure_utf8_stdio()      # 中文 Windows 上不这么做，doctor 打印 emoji/中文会 UnicodeEncodeError
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
