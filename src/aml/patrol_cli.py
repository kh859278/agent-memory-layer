"""`aml patrol` 子命令实现（技能治理 + 包版本监控 + 结尾播报）。"""
from __future__ import annotations

import json
import os

from . import kb
from .patrol import github, packages, skills, update
from .patrol.notify import NoticeQueue


def _patrol(cfg):
    return cfg.section("patrol")


def _ingest_skills(cfg, log=print, force: bool = False) -> dict | None:
    """把技能镜像目录增量灌进向量库（按上次灌库时间过滤 mtime）。"""
    marker = cfg.state_dir / "patrol" / "last_ingest.txt"
    since = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if since and not force:
        try:
            import datetime as dt
            if (dt.datetime.now() - dt.datetime.strptime(since, "%Y-%m-%d %H:%M:%S")).total_seconds() < 60:
                log("  刚刚灌过（<60s），跳过")
                return None
        except ValueError:
            since = ""
    info = kb.ingest_docs(cfg, dirs=["技能原始"], exts=[".md"], since=since or None,
                         progress=None if os.environ.get("AML_QUIET") else
                         (lambda d, t, ok, dup, err: log(f"     {d}/{t} 块 新增 {ok} 去重 {dup}")))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(dt_now(), encoding="utf-8")
    return info


def cmd_patrol_sync(cfg, args, log=print) -> int:
    """技能入库：镜像 + 重建清单（+ 可选灌向量库）。"""
    report = skills.mirror(cfg, dry_run=args.dry_run, delete=not args.no_delete)
    for label, info in report.items():
        log(f"  {label}: +{info['added_or_updated']} 改动 / -{info['removed']} 删除 → {info['target']}")
    if args.dry_run:
        for label, info in report.items():
            for rel in info.get("changed", [])[:10]:
                log(f"      {label}/{rel}")
        return 0
    inv = skills.write_inventory(cfg)
    log(f"  清单已重建：{inv['inventory']}（共 {inv['total']} 个技能，跟踪 {inv['tracked']}，"
        f"本地补丁 {inv['patched']}）")
    if args.ingest:
        info = _ingest_skills(cfg, log=log, force=args.force)
        if info:
            log(f"  灌库：{info['files']} 个文件 / {info['chunks']} 块 → "
                f"新增 {info['ok']} 去重 {info['dup']} 失败 {info['error']}")
    return 0


def cmd_patrol_adopt(cfg, args, log=print) -> int:
    result = update.adopt(cfg, log=log, dry_run=args.dry_run, apply_mode=args.apply,
                          repos=[x.strip() for x in args.repos.split(",") if x.strip()] or None)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_patrol_check(cfg, args, log=print) -> int:
    results, details = update.check(cfg, log=log, check_only=True, deep=args.deep)
    print("\n".join(details))
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def cmd_patrol_update(cfg, args, log=print) -> int:
    results, details = update.check(cfg, log=log, check_only=False, deep=args.deep)
    print("\n".join(details))
    updated = results["updated"]
    if updated and not args.no_sync:
        log("同步知识库（镜像 + 清单 + 增量灌向量库）...")
        skills.mirror(cfg)
        skills.write_inventory(cfg)
        info = _ingest_skills(cfg, log=log)
        if info:
            log(f"  灌库：{info['files']} 个文件 / {info['chunks']} 块 → 新增 {info['ok']}")
    if not args.no_notify:
        queue = NoticeQueue(cfg)
        parts = []
        if updated:
            shown = "、".join(updated[:4]) + ("…" if len(updated) > 4 else "")
            parts.append(f"技能自动更新 {len(updated)} 个（{shown}）")
        staged = results["staged"] + results["patched"]
        if staged:
            parts.append(f"{len(staged)} 个待复核已暂存")
        if parts:
            queue.add("skills", "；".join(parts),
                      detail=f"报告目录：{cfg.state_dir / 'patrol' / 'reports'}")
            log(f"  已入播报队列：{queue.brief()}")
    return 0


def cmd_patrol_accept(cfg, args, log=print) -> int:
    result = update.accept(cfg, name=args.name, all_=args.all, log=log)
    if result["accepted"]:
        NoticeQueue(cfg).add("skills", f"已采纳 {len(result['accepted'])} 个暂存的上游技能版本")
    else:
        log("没有可采纳的暂存项")
    return 0


def cmd_patrol_packages(cfg, args, log=print) -> int:
    # 这个函数既被 `aml patrol packages` 调，也被 `aml patrol run` 调；
    # 两个子命令的参数集不同，所以一律用 getattr 取默认值（踩过 AttributeError）。
    dry_run = getattr(args, "dry_run", False)
    no_notify = getattr(args, "no_notify", False)
    result = packages.check(cfg, log=log, write=not dry_run)
    if not no_notify and not dry_run:
        queue = NoticeQueue(cfg)
        for pkg, info in result.items():
            if info.get("action") == "notify":
                queue.add("packages", f"{pkg} 有新版 {info['target']}"
                                      f"（本机 {info['current']}）：{info['summary'][:60]}",
                          detail=f"升级是手动动作：{info.get('notes_url', '')}")
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_patrol_notify(cfg, args, log=print) -> int:
    queue = NoticeQueue(cfg)
    if args.brief:
        text = queue.brief(args.limit)
        if text:
            print(text)
        return 0
    if args.ack:
        count = queue.ack(notice_id=args.id, all_=args.all)
        print(f"已标记 {count} 条为已播报")
        return 0
    print(queue.render())
    return 0


def cmd_patrol_run(cfg, args, log=print) -> int:
    """定时任务调这个：纳管（可选）→ 检查/更新 → 镜像入库 → 包版本 → 播报。

    每个阶段单独兜异常：**一轮里某个阶段失败（网络抖、某个仓库挂了）不该把整轮搞黄**，
    否则连"镜像 + 播报"这种跟网络无关的事也做不成了。
    """
    patrol = _patrol(cfg)
    if not patrol.get("enabled", True):
        log("patrol 在配置里被禁用（patrol.enabled=false）")
        return 0

    failures = []

    def phase(title, fn, *a, **kw):
        import time
        started = time.time()
        log(title)
        try:
            result = fn(*a, **kw)
            log(f"  ⏱ {title.strip('= ')} 用时 {time.time() - started:.1f}s")
            return result
        except Exception as e:  # noqa: BLE001
            failures.append(f"{title.strip('= ')}: {type(e).__name__} {str(e)[:80]}")
            log(f"  ⚠ 本阶段失败（不中断整轮）：{type(e).__name__} {str(e)[:120]}")
            return None

    stamp = cfg.state_dir / "patrol" / "last_adopt.txt"
    need_adopt = not stamp.is_file()
    if stamp.is_file():
        import datetime as dt
        need_adopt = (dt.datetime.now() - dt.datetime.fromtimestamp(stamp.stat().st_mtime)).days >= 7
    if need_adopt and not getattr(args, "no_adopt", False):
        phase("=== 1/4 纳管新技能（每周最多一次）===", update.adopt, cfg, log=log)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(dt_now(), encoding="utf-8")
    else:
        log("=== 1/4 纳管：7 天内跑过，跳过 ===")

    phase("=== 2/4 技能监控 + 自动更新 ===", cmd_patrol_update, cfg, args, log=log)

    def _mirror():
        report = skills.mirror(cfg)
        for label, info in report.items():
            log(f"  {label}: +{info['added_or_updated']} / -{info['removed']}")
        log(f"  {skills.write_inventory(cfg)}")

    phase("=== 3/4 知识库镜像 + 清单 ===", _mirror)
    phase("=== 4/4 包版本监控 ===", cmd_patrol_packages, cfg, args, log=log)

    brief = NoticeQueue(cfg).brief()
    log(f"本轮播报内容：{brief or '（无变化）'}")
    if failures:
        log(f"本轮有 {len(failures)} 个阶段失败：{'；'.join(failures)}")
    return 0


def dt_now() -> str:
    import datetime as dt
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


__all__ = ["cmd_patrol_sync", "cmd_patrol_adopt", "cmd_patrol_check", "cmd_patrol_update",
           "cmd_patrol_accept", "cmd_patrol_packages", "cmd_patrol_notify", "cmd_patrol_run",
           "github", "skills", "update", "packages"]
