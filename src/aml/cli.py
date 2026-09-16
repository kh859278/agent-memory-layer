"""命令行入口：aml <子命令>。

设计上 CLI 只做编排，逻辑都在各模块里，方便测试与二次开发。
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
    if getattr(args, "home", None):
        overrides["aml_home"] = args.home
    if getattr(args, "api", None):
        overrides["memory_api"] = args.api
    if getattr(args, "db", None):
        overrides["db_path"] = args.db
    return config_mod.load(overrides)


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
    print("  3) aml sync        # 采集入库")
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
    if not args.dry_run and not args.no_service_check:
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
                              do_backfill=not args.no_backfill, progress=None if args.quiet else progress)
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
    retriever = Retriever(cfg)
    result = retriever.search(args.query, phase=args.phase, project=args.project,
                              tag=args.tag, n=args.n, allow_repeat=args.allow_repeat)
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
    retriever = Retriever(cfg)
    print(json.dumps(retriever.diagnostics(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aml", description="跨 agent 记忆层：采集 / 蒸馏 / 分阶段检索 / 人面镜像")
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
    sp.add_argument("--no-service-check", action="store_true")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("search", help="分阶段检索（P0–P6，级联回退 + 未命中解释）")
    sp.add_argument("query")
    sp.add_argument("--phase", default="P2", help="P0–P6，决定条数与字数预算（默认 P2）")
    sp.add_argument("--project", help="优先本项目历史")
    sp.add_argument("--tag", help="收窄标签，如 domain:env-windows")
    sp.add_argument("-n", type=int, help="覆盖该阶段的条数上限")
    sp.add_argument("--allow-repeat", action="store_true", help="忽略同查询冷却")
    sp.add_argument("--explain", action="store_true", help="额外打印诊断信息")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("index", help="知识库索引：show / rebuild")
    sp.add_argument("action", choices=["show", "rebuild"], nargs="?", default="show")
    sp.set_defaults(func=cmd_index)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
