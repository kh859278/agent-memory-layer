"""技能来源：`sources.json` + 仓库布局识别。

为什么要它（用户的第 ② 条）：现在的 `adopt` 是**猜**上游 —— "目录名命中 ≥3 个才认这个仓库"，
而真实仓库的布局五花八门（技能在 `skills/<名>`、在仓库根、在 `template/<名>`、
在 `plugins/x/skills/<名>`……）。猜错的代价是：把不属于该仓库的技能认领了，
之后自动更新就会拿别人的内容覆盖你的文件。

所以改成**显式来源 + 布局识别**：

```json
{"sources": [
  {"repo": "mattpocock/skills", "scope": "global", "layout": "standard",
   "subdir": null, "branch": null, "enabled": true, "priority": 100,
   "added_at": "2026-09-18 10:00:00"}
]}
```

* `layout: auto` 用识别结果；显式写 `standard/template/root/flat/nested` 就只认那一类
* `enabled: false` = 暂停监控（已装的技能原地不动，也不参与更新）
* `priority` 小的先匹配（同一个技能名在多个来源里出现时，先到先得，与 `skills.all_skills` 同口径）
* 文件不存在时用配置里的 `scopes[].sources` / 老 `candidate_repos` **播种**一份，之后以文件为准
  （避免"配置和文件两套真相"）
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from . import skills

LAYOUTS = ("auto", "standard", "categorized", "template", "root", "flat", "nested")


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_path(cfg):
    return cfg.state_dir / "patrol" / "sources.json"


def key_of(source: dict) -> str:
    """来源的唯一键：仓库 + 可选子目录。"""
    sub = source.get("subdir")
    return f"{source.get('repo')}#{sub}" if sub else str(source.get("repo") or "")


def seed_sources(cfg) -> list:
    """从配置播种来源（老的 `candidate_repos` 也认）。"""
    out, seen = [], set()
    from . import scopes
    for scope in scopes.load_scopes(cfg):
        for entry in scope.get("sources") or []:
            repo = entry.get("repo") if isinstance(entry, dict) else str(entry)
            if not repo or repo in seen:
                continue
            seen.add(repo)
            item = dict(entry) if isinstance(entry, dict) else {"repo": repo}
            item.setdefault("scope", scope["name"])
            out.append(item)
    for repo in cfg.section("patrol").get("candidate_repos") or []:
        if repo and repo not in seen:
            seen.add(repo)
            out.append({"repo": repo, "scope": "global", "seed": "candidate_repos"})
    for item in out:
        item.setdefault("layout", "auto")
        item.setdefault("enabled", True)
        item.setdefault("priority", 100)
        item.setdefault("added_at", _now())
    return out


def load(cfg, persist_seed: bool = True) -> list:
    """读来源；文件不存在就用配置播种（并落盘，之后以文件为准）。"""
    path = default_path(cfg)
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("sources") if isinstance(data, dict) else data
            return [dict(x) for x in (items or [])]
        except (OSError, ValueError):
            return []
    seeded = seed_sources(cfg)
    if seeded and persist_seed:
        save(cfg, seeded)
    return seeded


def save(cfg, sources: list) -> str:
    path = default_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"updated_at": _now(), "sources": sources}, f, ensure_ascii=False, indent=2)
    return str(path)


def get(cfg, key: str) -> dict | None:
    for source in load(cfg):
        if key_of(source) == key or source.get("repo") == key:
            return source
    return None


def add(cfg, repo: str, scope: str = "global", layout: str = "auto", subdir: str | None = None,
        branch: str | None = None, priority: int = 100, enabled: bool = True,
        log=print) -> dict:
    if layout not in LAYOUTS:
        raise ValueError(f"layout 必须是 {LAYOUTS} 之一，收到 {layout!r}")
    sources = load(cfg)
    item = {"repo": repo, "scope": scope, "layout": layout, "subdir": subdir, "branch": branch,
            "priority": int(priority), "enabled": bool(enabled), "added_at": _now()}
    for index, existing in enumerate(sources):
        if key_of(existing) == key_of(item):
            sources[index] = {**existing, **item}
            save(cfg, sources)
            log(f"  来源已更新：{key_of(item)}")
            return sources[index]
    sources.append(item)
    save(cfg, sources)
    log(f"  来源已添加：{key_of(item)}（layout {layout}）")
    return item


def _matches(source: dict, key: str) -> bool:
    """匹配口径：带 `#` 的键**精确到子目录**；只给 repo 就是"这个仓库的全部子来源"。

    第一版把两者混在一起（`repo == key` 或 `key_of == key`），结果按子目录精确删除时
    把同仓库的别的来源一起删了 —— 测试抓到的。
    """
    if "#" in key:
        return key_of(source) == key
    return source.get("repo") == key or key_of(source) == key


def remove(cfg, key: str, log=print) -> bool:
    sources = load(cfg)
    kept = [x for x in sources if not _matches(x, key)]
    if len(kept) == len(sources):
        return False
    save(cfg, kept)
    log(f"  来源已移除：{key}（已装的技能不会被删）")
    return True


def set_enabled(cfg, key: str, enabled: bool, log=print) -> bool:
    sources = load(cfg)
    hit = False
    for source in sources:
        if _matches(source, key):
            source["enabled"] = bool(enabled)
            hit = True
    if hit:
        save(cfg, sources)
        log(f"  {key}：{'启用' if enabled else '暂停'}监控（已装技能原地不动）")
    return hit


def enabled_sources(cfg, scope: str | None = None) -> list:
    """按优先级排序的启用来源。"""
    items = [x for x in load(cfg) if x.get("enabled", True)]
    if scope:
        items = [x for x in items if (x.get("scope") or "global") == scope]
    return sorted(items, key=lambda x: (int(x.get("priority", 100)), str(x.get("repo"))))


# ------------------------------------------------------------------ 布局识别

def classify(rel: str) -> str:
    """把技能目录相对仓库根的路径归类成布局。

    `categorized` 是实测加进来的：`mattpocock/skills` 的真实结构是
    `skills/<类别>/<技能名>`（如 `skills/engineering/tdd`），没有这一档时会被笼统报成
    `nested`，看报告的人分不清"这是它本来的组织方式"还是"藏在某个角落"。
    """
    rel = (rel or "").strip("/")
    if rel in ("", "."):
        return "root"
    parts = rel.split("/")
    if len(parts) == 1:
        return "flat"
    if parts[0] == "skills" and len(parts) == 2:
        return "standard"
    if parts[0] == "skills" and len(parts) == 3:
        return "categorized"
    if parts[0] == "template" and len(parts) == 2:
        return "template"
    return "nested"


def _name_of(path: str, rel: str) -> str:
    """技能名：优先 SKILL.md 的 front-matter name，取不到就用目录名。"""
    stem, _ = skills.read_front_matter(os.path.join(path, "SKILL.md"))
    return (stem or os.path.basename(rel.rstrip("/")) or rel).strip()


def detect_layouts(root: str) -> dict:
    """扫一个仓库快照：认出所有技能目录 + 布局分布。"""
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skills.SKIP_DIRS]
        if "SKILL.md" not in files:
            continue
        rel = os.path.relpath(base, root).replace("\\", "/")
        found.append({"name": _name_of(base, rel), "subdir": "" if rel == "." else rel,
                      "layout": classify(rel)})
    found.sort(key=lambda x: (x["layout"], x["subdir"]))
    counts = {}
    for item in found:
        counts[item["layout"]] = counts.get(item["layout"], 0) + 1
    return {"skills": found, "layouts": counts}


def norm(name: str) -> str:
    """名字归一化：小写、去掉非字母数字（`Skill-Name` == `skill_name`）。"""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def enumerate_source(source: dict, root: str, detected: dict | None = None) -> dict:
    """一个来源能提供哪些技能：{技能名: 子目录}（受 layout / subdir 限制）。"""
    detected = detected or detect_layouts(root)
    subdir = (source.get("subdir") or "").strip("/")
    layout = source.get("layout") or "auto"
    out = {}
    for item in detected["skills"]:
        rel = item["subdir"]
        if subdir and not (rel == subdir or rel.startswith(subdir + "/")):
            continue
        if layout != "auto" and item["layout"] != layout:
            continue
        out[item["name"]] = rel
    return out


def match_plan(local_names, remote_map: dict) -> dict:
    """把本地技能名匹配到远端：{本地名: 远端子目录}（归一化后精确匹配，不猜）。"""
    index = {}
    for remote_name, subdir in remote_map.items():
        index.setdefault(norm(remote_name), subdir)
    return {name: index[norm(name)] for name in local_names if norm(name) in index}


def render(cfg) -> str:
    sources = load(cfg)
    if not sources:
        return "还没有配置任何来源（`aml patrol sources add owner/repo`）"
    lines = [f"技能来源（{len(sources)} 个，按优先级）："]
    for source in sorted(sources, key=lambda x: (int(x.get("priority", 100)),
                                                 str(x.get("repo")))):
        state = "启用" if source.get("enabled", True) else "暂停"
        extra = []
        if source.get("layout") and source["layout"] != "auto":
            extra.append(f"layout={source['layout']}")
        if source.get("subdir"):
            extra.append(f"subdir={source['subdir']}")
        if source.get("branch"):
            extra.append(f"branch={source['branch']}")
        if source.get("seed"):
            extra.append(f"来自配置 {source['seed']}")
        lines.append(f"  [{state}] p{source.get('priority', 100):<4} "
                     f"{source.get('repo')}  scope={source.get('scope') or 'global'}"
                     + ("  " + " ".join(extra) if extra else ""))
    return "\n".join(lines)


def render_layouts(report: dict, repo: str = "", head: str = "") -> str:
    lines = [f"{repo}{'@' + head if head else ''}：共 {len(report['skills'])} 个技能目录"]
    for layout, count in sorted(report["layouts"].items()):
        lines.append(f"  layout {layout:<9} {count:>3} 个")
    for item in report["skills"][:12]:
        lines.append(f"    [{item['layout']:<9}] {item['name']:<28} {item['subdir'] or '.'}")
    if len(report["skills"]) > 12:
        lines.append(f"    …（还有 {len(report['skills']) - 12} 个）")
    return "\n".join(lines)


__all__ = ["LAYOUTS", "default_path", "key_of", "seed_sources", "load", "save", "get", "add",
           "remove", "set_enabled", "enabled_sources", "classify", "detect_layouts", "norm",
           "enumerate_source", "match_plan", "render", "render_layouts"]
