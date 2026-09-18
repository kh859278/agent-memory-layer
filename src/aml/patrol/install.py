"""技能安装/卸载：从 Git 仓库按**布局**取技能，装进指定作用域的目录。

这是对标那四个工具最核心的一块（它们的共同点就是**安装/更新/卸载**这一条生命周期），
但落点不一样：它们默认"装到所有 agent 的目录"，我们默认**只装到你指定的作用域目录**，
并且每一装都留下"从哪来、哪次提交、装到哪、指纹多少"（`_meta` + 锁文件），
这样更新、回滚、审计才有依据。

三段式，方便测试与 `--dry-run`：

    plan = plan_install(cfg, ["tdd"], scope="global", source="owner/repo")
    print(plan["picks"], plan["missing"])
    report = apply_plan(cfg, plan, dry_run=True)

`plan_install` 只读（不写文件、不改目录），`apply_plan` 才动东西；
`--dry-run` 就是"只 plan 不 apply"。

安全约定（沿用现有三条闸门的精神）：
  · 覆盖前**先备份**（`skills.backup_skill`），失败不半途而废：每个技能单独 try
  · 目标目录里已有同名技能且**本地改动过**（指纹不符）→ 默认不覆盖，进 `blocked`（要 `--force`）
  · 卸载也是"备份后删"，永远不会出现"删了就回不来"
"""
from __future__ import annotations

import os
import shutil

from . import github, scopes, skills, sources


def target_roots(cfg, scope: dict, create: bool = False) -> list:
    """这次装到哪些目录：该作用域下存在的根；一个都不存在就用第一个并（可选）建出来。"""
    rows = [r for r in scopes.all_roots(cfg) if r["scope"] == scope["name"]]
    if not rows:
        raise KeyError(f"作用域 {scope['name']} 没有配置技能目录（检查 patrol.scopes/roots）")
    existing = [r for r in rows if r["exists"]]
    if existing:
        return existing
    first = rows[0]
    if create and first["path"]:
        os.makedirs(first["path"], exist_ok=True)
        first = dict(first, exists=True)
    return [first]


def _default_fetch(cfg, source: dict, force_refresh: bool = False):
    """默认取快照：走 github（**带 tarball 缓存与主机白名单**，见 github.fetch_repo）。"""
    return github.fetch_repo(cfg, source["repo"], source.get("branch"),
                             force_refresh=force_refresh)


def plan_install(cfg, names, scope_name: str | None = None, source_key: str | None = None,
                 project: str | None = None, fetch=None, force_refresh: bool = False) -> dict:
    """规划一次安装（只读）：装哪些技能、从哪个来源的哪个子目录、装到哪些目录。"""
    scope = scopes.resolve(cfg, scope_name, project)
    roots = target_roots(cfg, scope)
    wanted = list(dict.fromkeys(names or []))
    if not wanted:
        raise ValueError("没有指定要装的技能名")

    candidates = []
    if source_key:
        one = sources.get(cfg, source_key)
        if not one:
            raise KeyError(f"没有这个来源：{source_key}（先 `aml patrol sources add {source_key}`）")
        candidates = [one]
    else:
        candidates = sources.enabled_sources(cfg, scope=scope["name"]) or sources.enabled_sources(cfg)
    if not candidates:
        raise KeyError("没有任何可用来源：`aml patrol sources add owner/repo` 后再装")

    fetch = fetch or _default_fetch
    picks, missing, scanned = {}, list(wanted), []
    scratches = []
    try:
        for source in candidates:
            if not scanned:                       # 第一个来源都还没扫
                pass
            try:
                root, ref, scratch = fetch(cfg, source, force_refresh)
            except Exception as e:  # noqa: BLE001 - 某个来源拉不动不影响别的来源
                scanned.append({"source": sources.key_of(source), "error":
                                f"{type(e).__name__}: {str(e)[:120]}"})
                continue
            scratches.append(scratch)
            remote = sources.enumerate_source(source, root)
            matched = sources.match_plan(missing, remote)
            for name, subdir in matched.items():
                picks[name] = {"name": name, "repo": source["repo"], "subdir": subdir,
                               "branch": source.get("branch"), "commit": ref,
                               "source": sources.key_of(source),
                               "snapshot": os.path.join(root, subdir) if subdir else root,
                               "scratch": scratch}
            scanned.append({"source": sources.key_of(source), "skills": len(remote),
                            "matched": sorted(matched)})
            missing = [n for n in missing if n not in picks]
            if not missing:
                break
    finally:
        keep = [p["scratch"] for p in picks.values()]
        for scratch in scratches:
            if scratch and scratch not in keep:
                shutil.rmtree(scratch, ignore_errors=True)
    return {"scope": scope["name"], "scope_kind": scope["kind"], "roots": roots,
            "wanted": wanted, "picks": picks, "missing": missing, "scanned": scanned,
            "sync_kb_roots": [r["name"] for r in roots if r["sync_kb"]]}


def _conflict(root: dict, subdir: str) -> tuple:
    """目标里已有同名技能吗？本地改过吗？→ (存在?, 有本地改动?)"""
    dest = os.path.join(root["path"], subdir)
    if not os.path.isdir(dest):
        return False, False
    meta, _ = skills.read_meta(dest)
    current = skills.dir_hash(dest)
    if not meta:
        return True, True                      # 没元数据 = 不是我们装的，视为"别人的东西"
    recorded = meta.get("content_hash")
    return True, bool(meta.get("local_patch") or meta.get("local_diff")
                      or (recorded and current != recorded))


def apply_plan(cfg, plan: dict, dry_run: bool = False, force: bool = False,
               log=print) -> dict:
    """执行规划：备份 → 覆盖 → 写元数据。`dry_run` 只打印要做的事。"""
    out = {"installed": [], "replaced": [], "blocked": [], "failed": [], "dry_run": dry_run,
           "targets": {}, "missing": plan.get("missing") or [], "scope": plan["scope"]}
    for name, pick in sorted(plan["picks"].items()):
        subdir = pick["subdir"] or name
        for root in plan["roots"]:
            dest = os.path.join(root["path"], subdir)
            existed, dirty = _conflict(root, subdir)
            if existed and dirty and not force:
                out["blocked"].append(f"{name}@{root['name']}")
                log(f"  🔒 {name} → {root['name']}：本地有改动，不覆盖（要覆盖加 --force）")
                continue
            action = "替换" if existed else "新装"
            if dry_run:
                log(f"  · 将{action} {name} → {root['name']}/{subdir}"
                    f"（源 {pick['repo']}@{pick['commit'][:7] if pick['commit'] else '内容'}）")
                out["targets"].setdefault(name, []).append(root["name"])
                continue
            try:
                if existed:
                    skills.backup_skill(cfg, dest, name, f"pre-install-{pick['commit'][:7]}"
                                        if pick["commit"] else "pre-install")
                skills.sync_dir(pick["snapshot"], dest)
                meta = {"name": name, "repo": pick["repo"], "subdir": pick["subdir"],
                        "branch": pick["branch"], "commit": pick["commit"],
                        "installed_at": skills.today(), "last_checked": skills.today(),
                        "content_hash": skills.dir_hash(dest), "local_diff": False,
                        "source": pick["source"], "scope": plan["scope"],
                        "target": root["name"],
                        # 装进来的那一刻，上游指纹 == 本地指纹（之后的差异才是"本地改过"）
                        "upstream_hash": skills.dir_hash(dest)}
                skills.write_meta(dest, meta)
            except Exception as e:  # noqa: BLE001 - 一个目录失败不影响其它技能
                out["failed"].append(f"{name}@{root['name']}: {type(e).__name__} {str(e)[:80]}")
                log(f"  ❌ {name} → {root['name']}：{type(e).__name__} {str(e)[:80]}")
                continue
            out["targets"].setdefault(name, []).append(root["name"])
            (out["replaced"] if existed else out["installed"]).append(f"{name}@{root['name']}")
            log(f"  ✅ {name} → {root['name']}/{subdir}"
                f"（{action}，源 {pick['repo']}"
                f"@{pick['commit'][:7] if pick['commit'] else '（按内容）'}）")
    for pick in plan["picks"].values():
        if pick.get("scratch"):
            shutil.rmtree(pick["scratch"], ignore_errors=True)
    if out["missing"]:
        log(f"  ⚠ 这些技能在所有来源里都没找到：{', '.join(out['missing'])}")
    return out


def install(cfg, names, scope_name: str | None = None, source_key: str | None = None,
            project: str | None = None, dry_run: bool = False, force: bool = False,
            fetch=None, force_refresh: bool = False, log=print) -> dict:
    """一步到位：规划 + 执行。"""
    plan = plan_install(cfg, names, scope_name=scope_name, source_key=source_key,
                        project=project, fetch=fetch, force_refresh=force_refresh)
    if scope_name is None and project is None:
        log(f"  作用域：{plan['scope']}（装到 {', '.join(r['name'] for r in plan['roots'])}）")
    return apply_plan(cfg, plan, dry_run=dry_run, force=force, log=log)


def uninstall(cfg, names, scope_name: str | None = None, project: str | None = None,
              dry_run: bool = False, log=print) -> dict:
    """卸载：**先备份再删**（永远能回来），并清掉元数据所在目录。"""
    scope = scopes.resolve(cfg, scope_name, project)
    roots = [r for r in scopes.all_roots(cfg) if r["scope"] == scope["name"] and r["exists"]]
    out = {"removed": [], "missing": [], "dry_run": dry_run, "scope": scope["name"]}
    for name in names:
        found = False
        for root in roots:
            dest = os.path.join(root["path"], name)
            if not os.path.isdir(dest):
                continue
            found = True
            if dry_run:
                log(f"  · 将卸载 {name} ← {root['name']}（先备份）")
                out["removed"].append(f"{name}@{root['name']}")
                continue
            try:
                skills.backup_skill(cfg, dest, name, "pre-uninstall")
                shutil.rmtree(dest)
                out["removed"].append(f"{name}@{root['name']}")
                log(f"  🗑 {name} ← {root['name']}（已备份，可回滚）")
            except Exception as e:  # noqa: BLE001 - 一个目录失败不影响其它目录
                out.setdefault("failed", []).append(f"{name}@{root['name']}: {type(e).__name__}")
                log(f"  ❌ {name} ← {root['name']}：{type(e).__name__} {e}")
        if not found:
            out["missing"].append(name)
    if out["missing"]:
        log(f"  ⚠ 没找到：{', '.join(out['missing'])}（作用域 {scope['name']}）")
    return out


def render_plan(plan: dict) -> str:
    lines = [f"安装计划（作用域 {plan['scope']}）：",
             f"  目标目录：{', '.join(r['name'] for r in plan['roots'])}"
             + (f"　同步知识库：{', '.join(plan['sync_kb_roots'])}" if plan["sync_kb_roots"]
                else "　（都不同步知识库）")]
    for name, pick in sorted(plan["picks"].items()):
        lines.append(f"  ✅ {name:<24} ← {pick['repo']} / {pick['subdir'] or '.'}"
                     f" @{(pick['commit'] or '内容')[:7]}")
    if plan["missing"]:
        lines.append(f"  ⚠ 找不到：{', '.join(plan['missing'])}")
    for item in plan["scanned"]:
        note = item.get("error") or f"{item.get('skills')} 个技能可装，命中 {item.get('matched')}"
        lines.append(f"  来源 {item['source']}：{note}")
    return "\n".join(lines)


__all__ = ["target_roots", "plan_install", "apply_plan", "install", "uninstall", "render_plan"]
