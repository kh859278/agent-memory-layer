"""锁文件与状态总览：把"每个技能从哪来、哪次提交、装在哪、指纹多少"落成一份可看的文件。

对标那两个工具的 `.skill-lock.json` / `skills-lock.json`：
  · `agent-skills-updater` 用锁文件记 install/update 时间
  · `biw/skills-updater` 更狠 —— 它的扫描器**把 `skills-lock.json` 当成"这是个技能项目"的标记**

我们的 per-skill `.skill-meta.json` 其实是超集（还记上游指纹、本地指纹、local_patch），
但缺"一份汇总"：别人（和别的机器、别的工具）看不到全貌，也没法一眼判断"哪些技能该更新"。

所以这里做两件事：
  1. `build()` / `write()`：导出一份 **v1 锁文件**（字段名尽量贴近他们，方便互通）
  2. `status_rows()`：`aml patrol status` 用的人类视图 —— 装了什么、谁改过、谁待批、谁没纳管

字段口径（写清免得以后自己都看不懂）：
    commit        上次同步时上游的提交（拿不到 sha 时是 None，此时看 upstream_hash）
    content_hash  当前**本地**内容指纹
    upstream_hash 上次同步时**上游**内容指纹 —— 两者不等 = 本地被改过
    targets       这个作用域里哪些目录装着它（一个技能可以同时装进多个 agent 目录）
"""
from __future__ import annotations

import datetime as dt
import json
import os

from . import scopes, skills

LOCK_VERSION = 1


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def generator() -> str:
    try:
        from importlib.metadata import version
        return f"agent-memory-layer/{version('agent-memory-layer')}"
    except Exception:  # noqa: BLE001 - 没装成包（源码直跑）时也要能出锁文件
        return "agent-memory-layer/dev"


def build(cfg) -> dict:
    """汇总所有技能 → 锁文件结构（只读）。"""
    out = {"lockfileVersion": LOCK_VERSION, "generated_at": _now(), "generator": generator(),
           "scopes": {}, "skills": {}}
    for scope in scopes.load_scopes(cfg):
        roots = [r["name"] for r in scopes.all_roots(cfg) if r["scope"] == scope["name"]]
        out["scopes"][scope["name"]] = {
            "kind": scope["kind"], "path": scope.get("path"), "roots": roots,
            "sync_kb": [r["name"] for r in scopes.all_roots(cfg)
                        if r["scope"] == scope["name"] and r["sync_kb"]]}
    for name, path, label in skills.all_skills(cfg):
        meta, _ = skills.read_meta(path)
        meta = meta or {}          # read_meta 对"没有元数据"的技能返回 None（不是 {}），别在这里炸
        root = scopes.of_skill(cfg, name) or {}
        entry = {
            "repo": meta.get("repo"), "subdir": meta.get("subdir"),
            "branch": meta.get("branch"), "commit": meta.get("commit"),
            "content_hash": skills.dir_hash(path),
            "upstream_hash": meta.get("upstream_hash"),
            "local_diff": bool(meta.get("local_diff")),
            "local_patch": bool(meta.get("local_patch")),
            "installed_at": meta.get("installed_at"), "last_checked": meta.get("last_checked"),
            "scope": root.get("scope") or "(未纳管)",
            "targets": sorted({r["name"] for r in scopes.all_roots(cfg)
                               if r["exists"] and os.path.isdir(os.path.join(r["path"], name))}),
            "synced_to_kb": bool(root.get("sync_kb")),
            "source_dir": label,
        }
        existing = out["skills"].get(name)
        if existing:                     # 同名技能装在多个作用域：合并 targets，标注多作用域
            existing["targets"] = sorted(set(existing["targets"]) | set(entry["targets"]))
            scopes_seen = set(str(existing["scope"]).split("+")) | {entry["scope"]}
            existing["scope"] = "+".join(sorted(scopes_seen))
            continue
        out["skills"][name] = entry
    return out


def default_path(cfg, scope: dict | None = None):
    """锁文件写哪：项目作用域写进**项目根**（让别的工具/别的机器能发现这个项目），
    全局写进知识库根（人能直接看到，也随知识库一起走）。"""
    from pathlib import Path
    name = str(cfg.section("patrol").get("lock_name") or "skills-lock.json")
    if scope and scope.get("kind") == "project" and scope.get("path"):
        return Path(scope["path"]) / name
    return cfg.knowledge_dir / name


def write(cfg, path=None, scope=None, log=print) -> str:
    """导出锁文件（默认知识库根；`--project` 场景可指定路径）。"""
    data = build(cfg)
    target = str(path) if path else str(default_path(cfg, scope))
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"  锁文件已写出：{target}（{len(data['skills'])} 个技能，"
        f"{len(data['scopes'])} 个作用域）")
    return target


def status_rows(cfg) -> list:
    """每个技能一行：装在哪、来自哪、有没有本地改动、是否待批、生命周期状态。"""
    from . import capability
    pending = {item["name"] for item in skills.pending_items(cfg)}
    rows = []
    for name, path, label in skills.all_skills(cfg):
        meta, _ = skills.read_meta(path)
        meta = meta or {}          # read_meta 对"没有元数据"的技能返回 None（不是 {}），别在这里炸
        root = scopes.of_skill(cfg, name) or {}
        current = skills.dir_hash(path)
        dirty = bool(meta.get("local_diff") or meta.get("local_patch")
                     or (meta.get("content_hash") and current != meta["content_hash"]))
        rows.append({
            "name": name, "scope": root.get("scope") or "(未纳管)", "dir": path,
            "source_dir": label, "repo": meta.get("repo") or "(未纳管)",
            "commit": (meta.get("commit") or "")[:7] or "-",
            "local_diff": dirty, "local_patch": bool(meta.get("local_patch")),
            "staged": name in pending, "synced_to_kb": bool(root.get("sync_kb")),
            "state": capability.state_of(cfg, name),
            "last_checked": meta.get("last_checked") or "-",
            "tracked": bool(meta.get("repo")),
        })
    return rows


def render_status(cfg) -> str:
    rows = status_rows(cfg)
    if not rows:
        return "没有发现任何技能（检查 patrol.scopes / skill_roots 配置）"
    tracked = [r for r in rows if r["tracked"]]
    dirty = [r for r in rows if r["local_diff"]]
    staged = [r for r in rows if r["staged"]]
    untracked = [r for r in rows if not r["tracked"]]
    lines = [f"技能状态（共 {len(rows)} 个）：已纳管 {len(tracked)}、"
             f"本地有改动 {len(dirty)}、待批暂存 {len(staged)}、未纳管 {len(untracked)}"]
    for row in sorted(rows, key=lambda r: (r["scope"] != "global", r["name"]))[:60]:
        flags = []
        if not row["tracked"]:
            flags.append("未纳管")
        if row["local_patch"]:
            flags.append("本地补丁")
        elif row["local_diff"]:
            flags.append("本地改动")
        if row["staged"]:
            flags.append("待批")
        if row["synced_to_kb"]:
            flags.append("进知识库")
        lines.append(f"  [{row['state']:<11}] {row['name']:<26} {row['repo']}@{row['commit']:<8} "
                     f"{'；'.join(flags) or '干净'}")
    if len(rows) > 60:
        lines.append(f"  …（还有 {len(rows) - 60} 个，用 --json 看全量）")
    if dirty:
        lines.append("  提示：本地有改动的技能上游更新时**只暂存不覆盖**；"
                     "要看差在哪就用 `aml patrol diff <技能名>`")
    return "\n".join(lines)


def render_lock(cfg) -> str:
    data = build(cfg)
    lines = [f"锁文件（v{data['lockfileVersion']}，{len(data['skills'])} 个技能）",
             f"  生成器 {data['generator']}　生成于 {data['generated_at']}"]
    for name, item in sorted(data["skills"].items())[:40]:
        mark = "有本地改动" if item["local_diff"] or item["local_patch"] else "干净"
        lines.append(f"  {name:<26} {(item['repo'] or '(未纳管)'):<24}"
                     f"{item['subdir'] or '-'} @{(item['commit'] or '-')[:7]:<8} {mark}")
    if len(data["skills"]) > 40:
        lines.append(f"  …（还有 {len(data['skills']) - 40} 个）")
    return "\n".join(lines)


__all__ = ["LOCK_VERSION", "generator", "build", "default_path", "write", "status_rows",
           "render_status", "render_lock"]
