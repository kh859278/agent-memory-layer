"""技能目录的通用操作：指纹、比较、元数据、备份/暂存、镜像、清单生成。

几个关键约定（都是从实战里定下来的，改之前先看原因）：

* **指纹用归一化后的内容**（CRLF→LF），否则同一个文件在 Windows/Unix 上哈希不同，
  会永远被判成"有变化"。
* **元数据 `.skill-meta.json`** 记 `repo / subdir / branch / commit /
  content_hash（本地指纹）/ upstream_hash（上游指纹）/ local_diff / local_patch`。
  没有它就只能靠猜上游；有了它才能在"上游变了"时判断**本地动过没有**。
* **镜像到知识库时统一把 mtime 设成同步时刻**：知识库的增量灌库（`--since`）靠 mtime，
  直接 copy2 会把上游几个月前的 mtime 带过来，新内容会被当成旧的跳过（踩过）。
"""
from __future__ import annotations

import datetime as dt
import filecmp
import hashlib
import json
import os
import shutil

META_NAMES = (".skill-meta.json", ".huashu-skill-meta.json", "source.json")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache", ".venv"}
SKIP_EXTS = {".pyc", ".pyo"}
META_FILES = set(META_NAMES) | {".last-update-check"}


# ------------------------------------------------------------------ 基础

def today() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def skill_files(root: str) -> dict:
    """{相对路径(posix): 绝对路径}，跳过噪声与元数据文件。"""
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() in SKIP_EXTS or name in META_FILES:
                continue
            path = os.path.join(base, name)
            out[os.path.relpath(path, root).replace("\\", "/")] = path
    return out


def _norm(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def dir_hash(root: str) -> str:
    digest = hashlib.sha256()
    for rel, path in sorted(skill_files(root).items()):
        digest.update(rel.encode("utf-8"))
        try:
            with open(path, "rb") as f:
                digest.update(_norm(f.read()))
        except OSError:
            pass
    return digest.hexdigest()[:16]


def compare_dirs(a: str, b: str) -> dict:
    """{'only_a': 只在 A, 'only_b': 只在 B, 'differ': 内容不同}"""
    fa, fb = skill_files(a), skill_files(b)
    only_a = sorted(set(fa) - set(fb))
    only_b = sorted(set(fb) - set(fa))
    differ = []
    for rel in sorted(set(fa) & set(fb)):
        try:
            if not filecmp.cmp(fa[rel], fb[rel], shallow=False):
                with open(fa[rel], "rb") as f1, open(fb[rel], "rb") as f2:
                    if _norm(f1.read()) != _norm(f2.read()):
                        differ.append(rel)
        except OSError:
            differ.append(rel)
    return {"only_a": only_a, "only_b": only_b, "differ": differ}


def diff_preview(a: str, b: str, max_lines: int = 12) -> list:
    import difflib
    lines = []
    fa, fb = skill_files(a), skill_files(b)
    for rel in sorted(set(fa) & set(fb)):
        try:
            ta = _norm(open(fa[rel], "rb").read()).decode("utf-8", "replace").splitlines()
            tb = _norm(open(fb[rel], "rb").read()).decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        if ta == tb:
            continue
        lines += list(difflib.unified_diff(ta, tb, fromfile=f"本地/{rel}", tofile=f"上游/{rel}",
                                           lineterm="", n=1))[:max_lines]
        if len(lines) >= max_lines:
            break
    return lines[:max_lines]


def sync_dir(src: str, dst: str) -> list:
    """把 src 的**内容**镜像进 dst（保留 .git 与元数据文件）；返回改动过的相对路径。

    统一把 mtime 设成当前时间（见模块 docstring）。
    """
    keep = META_FILES | {".git"}
    s_files, d_files = skill_files(src), skill_files(dst)
    changed = []
    for rel, sp in s_files.items():
        dp = os.path.join(dst, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dp), exist_ok=True)
        same = os.path.isfile(dp)
        if same:
            try:
                with open(sp, "rb") as f1, open(dp, "rb") as f2:
                    same = _norm(f1.read()) == _norm(f2.read())
            except OSError:
                same = False
        if not same:
            shutil.copy2(sp, dp)
            os.utime(dp, None)
            changed.append(rel)
    for rel, dp in d_files.items():
        if rel not in s_files and os.path.basename(rel) not in keep:
            try:
                os.remove(dp)
                changed.append(f"-{rel}")
            except OSError:
                pass
    return changed


# ---------------------------------------------------------------- 元数据

def meta_path(skill_dir: str) -> str:
    return os.path.join(skill_dir, ".skill-meta.json")


def read_meta(skill_dir: str):
    for name in META_NAMES:
        path = os.path.join(skill_dir, name)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f), path
            except (OSError, ValueError):
                return {}, path
    return None, None


def write_meta(skill_dir: str, meta: dict, path: str | None = None) -> str | None:
    """写元数据；写不进去只告警不中断（巡检宁可少记一次时间戳，不该整轮崩掉）。"""
    path = path or meta_path(skill_dir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def is_git_install(skill_dir: str) -> bool:
    return os.path.isdir(os.path.join(skill_dir, ".git"))


# ------------------------------------------------------------ 备份 / 暂存

def backup_skill(cfg, skill_dir: str, name: str, tag: str) -> str:
    root = cfg.state_dir / "patrol" / "_backup"
    dest = root / f"{name}-{tag}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(skill_dir, dest,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    prune_backups(cfg)
    return str(dest)


def stage_skill(cfg, src_dir: str, name: str, tag: str) -> str:
    dest = cfg.state_dir / "patrol" / "_pending" / f"{name}-{tag}"
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dest,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
    return str(dest)


def prune_backups(cfg, keep: int | None = None) -> list:
    keep = int(keep or cfg.section("patrol").get("update", {}).get("keep_backups", 10))
    root = cfg.state_dir / "patrol" / "_backup"
    if not root.is_dir():
        return []
    items = sorted((p for p in root.iterdir() if p.is_dir()),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for path in items[keep:]:
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
    return removed


def pending_items(cfg) -> list:
    root = cfg.state_dir / "patrol" / "_pending"
    if not root.is_dir():
        return []
    return [{"name": p.name.rsplit("-", 1)[0], "path": str(p)} for p in sorted(root.iterdir())
            if p.is_dir()]


# ------------------------------------------------------------- 发现技能

def live_roots(cfg) -> list:
    """[(根名, 绝对路径, 镜像目标相对知识库的路径)]，只返回存在的。"""
    out = []
    for entry in cfg.section("patrol").get("skill_roots") or []:
        path = os.path.expanduser(os.path.expandvars(str(entry.get("path", ""))))
        if path and os.path.isdir(path):
            out.append((entry.get("name") or os.path.basename(path), path,
                        entry.get("mirror_to") or f"技能原始/{entry.get('name')}"))
    return out


def discover_skills(root: str) -> list:
    """一个技能根下的所有技能目录（含 SKILL.md 才算）。"""
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "SKILL.md")):
            out.append((name, path))
    return out


def remote_skill_dirs(root: str) -> dict:
    """从解包好的仓库快照里找技能目录：{目录名: 相对路径}。"""
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        if "SKILL.md" in files:
            rel = os.path.relpath(base, root).replace("\\", "/")
            if rel != ".":
                out[rel.split("/")[-1]] = rel
    return out


def all_skills(cfg) -> list:
    """[(技能名, 目录, 来源根名)]，同名按配置顺序先到先得。"""
    seen, out = set(), []
    for label, root, _ in live_roots(cfg):
        for name, path in discover_skills(root):
            if name in seen:
                continue
            seen.add(name)
            out.append((name, path, label))
    return out


# -------------------------------------------------------- 清单 / 镜像

def read_front_matter(path: str) -> tuple:
    """极简 YAML front-matter 读取：只要 name 与 description。"""
    name = desc = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read(8000).splitlines()
    except OSError:
        return None, None
    if not lines or lines[0].strip() != "---":
        return None, None
    buf, key = [], None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1] not in (" ", "\t") and ":" in line:
            if key == "description" and desc is None:
                desc = " ".join(buf).strip()
            key = line.split(":", 1)[0].strip()
            value = line.split(":", 1)[1].strip()
            if key == "name":
                name = value.strip("\"'")
            buf = [value] if key == "description" else []
        elif key == "description":
            buf.append(line.strip())
    if key == "description" and desc is None:
        desc = " ".join(buf).strip()
    return name, (desc or "").strip("\"'")


def mirror(cfg, dry_run: bool = False, delete: bool = True) -> dict:
    """把活技能目录镜像进知识库。返回每个根的增/改/删统计。"""
    report = {}
    for label, root, rel in live_roots(cfg):
        target = cfg.knowledge_dir / rel
        if dry_run:
            changed = []
            for rel, sp in sorted(skill_files(root).items()):
                dp = os.path.join(target, rel.replace("/", os.sep))
                if not os.path.isfile(dp):
                    changed.append(rel)
                    continue
                try:
                    with open(sp, "rb") as f1, open(dp, "rb") as f2:
                        if _norm(f1.read()) != _norm(f2.read()):
                            changed.append(rel)
                except OSError:
                    changed.append(rel)
            removed = 0
            if delete and target.is_dir():
                removed = len([rel for rel in skill_files(str(target)) if rel not in skill_files(root)])
            report[label] = {"added_or_updated": len(changed), "removed": removed,
                             "target": str(target), "changed": changed[:20]}
            continue
        changed = sync_dir(root, str(target))
        report[label] = {"added_or_updated": len([c for c in changed if not c.startswith("-")]),
                         "removed": len([c for c in changed if c.startswith("-")]),
                         "target": str(target), "changed": changed[:20]}
    return report


def write_inventory(cfg, skills: list | None = None) -> dict:
    """重建技能清单（活目录优先，快照目录补充）。"""
    skills = skills if skills is not None else all_skills(cfg)
    snap_root = cfg.knowledge_dir / "技能原始"
    rows = {name: {"name": name, "dir": path, "sources": [label]} for name, path, label in skills}
    order = [name for name, _, _ in skills]
    snapshot_only = []
    for snap in cfg.section("patrol").get("snapshot_dirs") or []:
        for name, path in discover_skills(str(snap_root / snap)):
            if name in rows:
                rows[name]["sources"].append(f"{snap}(快照)")
            else:
                rows[name] = {"name": name, "dir": path, "sources": [f"{snap}(快照)"]}
                order.append(name)
                snapshot_only.append(name)

    tracked, patched, plain = [], [], []
    for name in order:
        row = rows[name]
        meta, _ = read_meta(row["dir"])
        row["meta"] = meta
        if meta:
            tracked.append(row)
            if meta.get("local_patch"):
                patched.append(row)
        else:
            plain.append(row)

    out_lines = ["# 技能清单（自动生成，勿手改）", "",
                 f"> 由 `aml patrol sync` 于 {dt.datetime.now():%Y-%m-%d %H:%M} 生成。",
                 "> 来源是**活技能目录**（agent 真正加载的那些），另有只读快照参与列示。", "",
                 f"- 技能总数：**{len(order)}**"
                 f"（仅存在于快照的 {len(snapshot_only)} 个）",
                 f"- 已纳入上游跟踪（可自动更新）：**{len(tracked)}**",
                 f"- 带本地补丁（**永不自动覆盖**）：{len(patched)}", "",
                 "## 一、可跟踪技能", "",
                 "| 技能 | 来源目录 | 上游 | 本地补丁 | 说明 |", "|------|----------|------|----------|------|"]
    for row in tracked:
        meta = row["meta"]
        upstream = str(meta.get("repo") or "?")
        if meta.get("subdir"):
            upstream += f" / {meta['subdir']}"
        _, desc = read_front_matter(os.path.join(row["dir"], "SKILL.md"))
        out_lines.append(f"| **{row['name']}** | {', '.join(row['sources'])} | `{upstream}` | "
                         f"{'⚠ 有' if meta.get('local_patch') else '—'} | {(desc or '')[:80]} |")
    out_lines += ["", "## 二、未跟踪技能（暂无上游来源，只能靠知识库快照留档）", "",
                  "| 技能 | 来源目录 | 说明 |", "|------|----------|------|"]
    for row in plain:
        _, desc = read_front_matter(os.path.join(row["dir"], "SKILL.md"))
        out_lines.append(f"| **{row['name']}** | {', '.join(row['sources'])} | {(desc or '')[:90]} |")
    out_lines += ["", "## 三、目录对照", "", "| 知识库目录 | 对应活目录 | 文件数 |", "|---|---|---|"]
    for _label, root, rel in live_roots(cfg):
        out_lines.append(f"| `{rel}/` | `{root}` | {len(skill_files(root))} |")
    for snap in cfg.section("patrol").get("snapshot_dirs") or []:
        path = snap_root / snap
        if path.is_dir():
            out_lines.append(f"| `技能原始/{snap}/` | （历史快照，只读） | {len(skill_files(str(path)))} |")

    inventory = cfg.knowledge_dir / str(cfg.section("patrol").get("inventory_relpath")
                                        or "技能/技能清单.md")
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text("\n".join(out_lines) + "\n", encoding="utf-8", newline="\n")
    return {"total": len(order), "tracked": len(tracked), "patched": len(patched),
            "snapshot_only": len(snapshot_only), "inventory": str(inventory)}
