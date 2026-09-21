"""`aml patrol` 子命令实现（技能治理 + 包版本监控 + 结尾播报）。"""
from __future__ import annotations

import json
import os
import sys

from . import kb
from .patrol import capability, github, packages, skills, update
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
        gated = results.get("gated") or []
        if gated:
            shown = "、".join(gated[:4]) + ("…" if len(gated) > 4 else "")
            parts.append(f"{len(gated)} 个被能力/生命周期闸门拦下待批（{shown}）")
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
        phase("=== 1/5 纳管新技能（每周最多一次）===", update.adopt, cfg, log=log)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(dt_now(), encoding="utf-8")
    else:
        log("=== 1/5 纳管：7 天内跑过，跳过 ===")

    phase("=== 2/5 技能监控 + 自动更新 ===", cmd_patrol_update, cfg, args, log=log)

    def _mirror():
        report = skills.mirror(cfg)
        for label, info in report.items():
            log(f"  {label}: +{info['added_or_updated']} / -{info['removed']}")
        log(f"  {skills.write_inventory(cfg)}")
        # 生命周期登记放在镜像之后：这时 meta（local_diff / staged_upstream）才是这一轮的新值
        capability.ensure(cfg, log=log)

    phase("=== 3/5 知识库镜像 + 清单 + 生命周期登记 ===", _mirror)
    phase("=== 4/5 包版本监控 ===", cmd_patrol_packages, cfg, args, log=log)

    def _state_guard():
        # state/ 里不可重建的那部分：写说明 + 打快照 + 报"丢没丢"
        # （9/18 整棵 state/ 被删掉重建过一次，所以这道收尾是必须的）
        from . import state_guard
        state_guard.write_readme(cfg)
        state_guard.snapshot(cfg, log=log)
        for item in state_guard.findings(cfg):
            log(f"  {item['detail']}")

    phase("=== 5/5 运行数据快照（不可重建的那些）===", _state_guard)

    brief = NoticeQueue(cfg).brief()
    log(f"本轮播报内容：{brief or '（无变化）'}")
    if failures:
        log(f"本轮有 {len(failures)} 个阶段失败：{'；'.join(failures)}")
    return 0


def cmd_patrol_diff(cfg, args, log=print) -> int:
    """看"上游/已暂存版到底改了什么"：正文 diff + 新增能力信号（网络/shell/密钥/写文件…）。"""
    from .patrol import diff as diff_mod
    report = diff_mod.collect(cfg, name=getattr(args, "name", None),
                              pending_only=not getattr(args, "fetch", False), log=log,
                              max_lines=getattr(args, "max_lines", 40))
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(diff_mod.render(report))
    if getattr(args, "fail_on_risk", False) and diff_mod.risky(report):
        log("⚠ 存在新增高风险能力信号（--fail-on-risk 生效，退出码 2）")
        return 2
    return 0


def cmd_patrol_capabilities(cfg, args, log=print) -> int:
    """技能能力：**声明 vs 实测**。没声明却在跑 shell/网络，是最该先补的。"""
    from .patrol import capability
    name = getattr(args, "name", None)
    if name:
        path = next((p for n, p, _ in skills.all_skills(cfg) if n == name), None)
        if not path:
            print(f"找不到技能：{name}", file=sys.stderr)
            return 2
        info = capability.compare(path)
        print(f"技能 {name}")
        print(f"  声明：{info['declared'] or '（没有 skill.yaml —— 无声明 ≠ 声明为空）'}")
        print(f"  实测：{ {k: len(v) for k, v in info['detected'].items()} or '（没扫到能力信号）'}")
        if info["undeclared_high_risk"]:
            print(f"  ⚠ 实测到但**没声明**的高风险能力：{', '.join(info['undeclared_high_risk'])}")
        elif info["undeclared"]:
            print(f"  实测到但没声明的能力：{', '.join(info['undeclared'])}")
        if info["declared_but_unused"]:
            print(f"  声明了但没扫到证据：{', '.join(info['declared_but_unused'])}")
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    report = capability.overview(cfg)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(capability.render(cfg))
    return 0


def cmd_patrol_lifecycle(cfg, args, log=print) -> int:
    """生命周期：不给 --set 就列出（并顺手登记新技能），给了就改（只允许合法迁移）。"""
    from .patrol import capability
    added = capability.ensure(cfg, log=log)
    name = getattr(args, "name", None)
    target = getattr(args, "target", None)
    if name and target:
        result = capability.set_state(cfg, name, target, why=args.why, by=args.by,
                                      force=args.force, log=log)
        if not result.get("ok"):
            print(f"没有改成：{result['error']}", file=sys.stderr)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    data = capability.overview(cfg)
    if name:
        row = next((r for r in data["skills"] if r["name"] == name), None)
        if not row:
            print(f"找不到技能：{name}", file=sys.stderr)
            return 2
        entry = (capability.load(cfg)["skills"] or {}).get(name) or {}
        row["history"] = entry.get("history") or []
        row["allowed_next"] = list(capability.TRANSITIONS.get(row["state"]) or [])
        if args.json:
            print(json.dumps(row, ensure_ascii=False, indent=2))
            return 0
        print(f"技能 {name}：状态 {row['state']}（自 {row.get('since') or '?'}）")
        print(f"  可以改成：{', '.join(row['allowed_next']) or '（终态）'}")
        print(f"  声明：{row['declared'] or '（无 skill.yaml）'}"
              + ("　需人工批准" if row["requires_approval"] else ""))
        if row["undeclared_high_risk"]:
            print(f"  ⚠ 未声明的高风险能力：{', '.join(row['undeclared_high_risk'])}")
        for item in row["history"][-8:]:
            print(f"  · {item.get('at')} {item.get('from')} → {item.get('to')}"
                  f"（{item.get('by')}）{item.get('why') or ''}"
                  + ("　[越级]" if item.get("forced") else ""))
        return 0
    if added and not args.json:
        log("")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(capability.render(cfg))
    return 0


def cmd_patrol_scopes(cfg, args, log=print) -> int:
    """作用域：技能装到哪些 agent 目录、哪些目录的正文也进知识库。"""
    from .patrol import scopes
    if getattr(args, "json", False):
        print(json.dumps(scopes.summary(cfg), ensure_ascii=False, indent=2))
        return 0
    print(scopes.render(cfg))
    return 0


def cmd_patrol_sources(cfg, args, log=print) -> int:
    """来源：多仓库 + 仓库结构（layout）识别。`--fetch` 才联网取快照来认结构。"""
    from .patrol import github, sources
    action = getattr(args, "action", None) or "list"
    if action == "list":
        if getattr(args, "json", False):
            print(json.dumps(sources.load(cfg), ensure_ascii=False, indent=2))
            return 0
        print(sources.render(cfg))
        return 0
    if action == "add":
        try:
            sources.add(cfg, args.repo, scope=getattr(args, "scope", None) or "global",
                        layout=getattr(args, "layout", None) or "auto",
                        subdir=getattr(args, "subdir", None),
                        branch=getattr(args, "branch", None),
                        priority=int(getattr(args, "priority", 100) or 100), log=log)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0
    if action == "remove":
        if not sources.remove(cfg, args.repo, log=log):
            print(f"没有这个来源：{args.repo}", file=sys.stderr)
            return 2
        return 0
    if action in ("enable", "disable"):
        if not sources.set_enabled(cfg, args.repo, action == "enable", log=log):
            print(f"没有这个来源：{args.repo}", file=sys.stderr)
            return 2
        return 0
    if action == "detect":
        if not getattr(args, "fetch", False):
            print("detect 需要 --fetch（要下仓库快照才能认结构）", file=sys.stderr)
            return 2
        try:
            root, used, scratch = github.fetch_repo(cfg, args.repo)
        except Exception as e:  # noqa: BLE001
            print(f"拉取失败：{type(e).__name__} {str(e)[:120]}", file=sys.stderr)
            return 1
        try:
            report = sources.detect_layouts(root)
            if getattr(args, "json", False):
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(sources.render_layouts(report, repo=args.repo, head=used))
        finally:
            import shutil as _shutil
            _shutil.rmtree(scratch, ignore_errors=True)
        return 0
    print(f"未知动作：{action}", file=sys.stderr)
    return 2


def cmd_patrol_install(cfg, args, log=print) -> int:
    """装技能：从来源仓库按布局取，装进作用域里的目标目录（默认只装到"活的"目录）。

    两种用法：`aml patrol install tdd grilling`（点名装）或 `--profile <名>`（按 profile 复现整套）。
    """
    from .patrol import install
    profile_name = getattr(args, "profile", None)
    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)
    if profile_name:
        try:
            total = install.install_from_profile(
                cfg, profile_name, project=getattr(args, "project", None), dry_run=dry_run,
                force=force, force_refresh=getattr(args, "force_refresh", False), log=log)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 2
        if getattr(args, "json", False):
            print(json.dumps({k: v for k, v in total.items()}, ensure_ascii=False, indent=2,
                             default=str))
        log(f"  profile {profile_name} 结果：新装 {len(total['installed'])}、"
            f"替换 {len(total['replaced'])}、拦下 {len(total['blocked'])}、"
            f"失败 {len(total['failed'])}、缺失 {len(total['missing'])}")
        return 1 if (total["failed"] or total["missing"] or total["blocked"]) else 0

    try:
        plan = install.plan_install(cfg, [n for n in (args.names or []) if n],
                                    scope_name=getattr(args, "scope", None),
                                    source_key=getattr(args, "source", None),
                                    project=getattr(args, "project", None),
                                    force_refresh=getattr(args, "force_refresh", False))
    except (KeyError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    if not getattr(args, "quiet", False):
        print(install.render_plan(plan))
    if getattr(args, "plan_only", False):
        return 0
    report = install.apply_plan(cfg, plan, dry_run=dry_run, force=force, log=log)
    if getattr(args, "json", False):
        print(json.dumps({k: v for k, v in report.items()}, ensure_ascii=False, indent=2,
                         default=str))
    if report["blocked"]:
        log(f"  {len(report['blocked'])} 个目标因本地有改动被拦下（--force 可覆盖，"
            f"或先 `aml patrol diff <技能>` 看看差在哪）")
    return 1 if (report["failed"] or report["missing"] or report["blocked"]) else 0


def cmd_patrol_uninstall(cfg, args, log=print) -> int:
    """卸技能：先备份再删（卸了也能从 backup 回滚）。"""
    from .patrol import install
    try:
        report = install.uninstall(cfg, [n for n in (args.names or []) if n],
                                   scope_name=getattr(args, "scope", None),
                                   project=getattr(args, "project", None),
                                   dry_run=getattr(args, "dry_run", False), log=log)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if report["missing"] else 0


def cmd_patrol_status(cfg, args, log=print) -> int:
    """状态总览：装了什么、谁有本地改动、谁待批、谁还没纳管。"""
    from .patrol import lockfile
    if getattr(args, "json", False):
        print(json.dumps(lockfile.status_rows(cfg), ensure_ascii=False, indent=2, default=str))
        return 0
    print(lockfile.render_status(cfg))
    return 0


def cmd_patrol_lock(cfg, args, log=print) -> int:
    """锁文件：导出"每个技能从哪来/哪次提交/装在哪/指纹多少"（给别的工具和别的机器看）。"""
    from .patrol import lockfile, scopes
    if not getattr(args, "write", False):
        if getattr(args, "json", False):
            print(json.dumps(lockfile.build(cfg), ensure_ascii=False, indent=2, default=str))
            return 0
        print(lockfile.render_lock(cfg))
        print("（只读模式。要落盘加 --write，可用 --out 指定路径）")
        return 0
    scope = None
    if getattr(args, "project", None) or getattr(args, "scope", None):
        try:
            scope = scopes.resolve(cfg, getattr(args, "scope", None), getattr(args, "project", None))
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 2
    path = lockfile.write(cfg, path=getattr(args, "out", None), scope=scope, log=log)
    if getattr(args, "json", False):
        print(json.dumps({"path": path}, ensure_ascii=False))
    return 0


def cmd_patrol_profile(cfg, args, log=print) -> int:
    """profile：把"要哪些技能 + 装到哪个作用域"记成可复现清单（非交互地复现整套技能集）。"""
    from .patrol import profiles
    action = getattr(args, "action", None) or "list"
    if action == "list":
        if getattr(args, "json", False):
            print(json.dumps(profiles.load(cfg), ensure_ascii=False, indent=2, default=str))
            return 0
        print(profiles.render(cfg))
        return 0
    if action == "save":
        if not args.name:
            print("需要 profile 名：`aml patrol profile save <名>`", file=sys.stderr)
            return 2
        try:
            profiles.snapshot(cfg, args.name, scope_name=getattr(args, "scope", None),
                              project=getattr(args, "project", None),
                              note=getattr(args, "note", "") or "", log=log)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0
    if action == "show":
        try:
            profile = profiles.get(cfg, args.name)
        except KeyError as e:
            print(str(e), file=sys.stderr)
            return 2
        if getattr(args, "json", False):
            print(json.dumps(profile, ensure_ascii=False, indent=2, default=str))
            return 0
        print(f"profile {args.name}：作用域 {profile.get('scope')}，"
              f"{len(profile.get('skills') or {})} 个技能")
        for source, items in sorted(profiles.groups(profile).items()):
            print(f"  ← {source or '(未指定来源)'}")
            for item in sorted(items):
                print(f"      {item}")
        return 0
    if action == "remove":
        if not profiles.remove(cfg, args.name, log=log):
            print(f"没有这个 profile：{args.name}", file=sys.stderr)
            return 2
        return 0
    print(f"未知动作：{action}", file=sys.stderr)
    return 2


def cmd_patrol_config(cfg, args, log=print) -> int:
    """配置同步：把 profile 推到远端 / 从远端拉（路径或 git 仓库）。"""
    from .patrol import profiles
    action = getattr(args, "action", None) or "show"
    remote = getattr(args, "remote", None) or cfg.section("patrol").get("config", {}).get("remote")
    if action == "show":
        if getattr(args, "json", False):
            print(json.dumps(profiles.export(cfg), ensure_ascii=False, indent=2, default=str))
            return 0
        print(profiles.render(cfg))
        print(f"  远端：{remote or '（未配置 patrol.config.remote）'}")
        return 0
    if action in ("push", "pull"):
        if not remote:
            print("没有远端：`aml patrol config push --remote <路径或 git URL>`，"
                  "或在配置里写 patrol.config.remote", file=sys.stderr)
            return 2
        try:
            if action == "push":
                result = profiles.push(cfg, remote, dry_run=getattr(args, "dry_run", False), log=log)
            else:
                result = profiles.pull(cfg, remote, dry_run=getattr(args, "dry_run", False), log=log)
        except PermissionError as e:
            print(str(e), file=sys.stderr)
            return 2
        except Exception as e:  # noqa: BLE001 - 网络/凭据问题要给人话，不是堆栈
            print(f"失败：{type(e).__name__} {str(e)[:200]}", file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps({k: v for k, v in result.items() if k != "remote"},
                             ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("ok") else 1
    print(f"未知动作：{action}", file=sys.stderr)
    return 2


def cmd_patrol_ui(cfg, args, log=print) -> int:
    """只读本地视图：把作用域/来源/生命周期/待批/最近报告汇成一页静态 HTML（不含技能正文）。"""
    from .patrol import ui
    if getattr(args, "print_only", False):
        print(ui.render_html(ui.build(cfg)))
        return 0
    path = ui.write(cfg, out=getattr(args, "out", None))
    log(f"  已生成只读视图：{path}")
    log("  （单文件、无外链、无 JS 依赖；用浏览器直接打开）")
    return 0


def cmd_self_update(cfg, args, log=print) -> int:
    """自查更新：默认**只查不装**（自升级是不可逆动作，且这台机器上它是计划任务）。

    `--source auto`（默认）先查 PyPI 并**校验归属**：PyPI 上的同名包是别人的（SAP 的），
    校验不过就退回查我们自己仓库的 tag。
    """
    from . import selfupdate
    source = getattr(args, "source", None) or "auto"
    report = selfupdate.plan(cfg, source=source)
    print(selfupdate.render(report))
    if not getattr(args, "apply", False):
        if report.get("needs_upgrade"):
            print("（只查不装。确认要升级再加 --apply —— 它会真的替换已安装的代码）")
        return 0
    if not report.get("needs_upgrade"):
        return 0
    result = selfupdate.apply(cfg, dry_run=getattr(args, "dry_run", False),
                              log=lambda m: print(m, flush=True))
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if not result.get("ok"):
        print(f"升级失败：{result.get('error') or result.get('rc')}", file=sys.stderr)
        return 1
    print("升级完成（下次运行 aml 生效）。技能与记忆层数据不受影响。")
    return 0


def dt_now() -> str:
    import datetime as dt
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


__all__ = ["cmd_patrol_sync", "cmd_patrol_adopt", "cmd_patrol_check", "cmd_patrol_update",
           "cmd_patrol_accept", "cmd_patrol_packages", "cmd_patrol_notify", "cmd_patrol_run",
           "cmd_patrol_diff", "cmd_patrol_capabilities", "cmd_patrol_lifecycle",
           "cmd_patrol_scopes", "cmd_patrol_sources", "cmd_patrol_install", "cmd_patrol_uninstall",
           "cmd_patrol_status", "cmd_patrol_lock", "cmd_patrol_profile", "cmd_patrol_config",
           "cmd_patrol_ui", "cmd_self_update",
           "github", "skills", "update", "packages"]
