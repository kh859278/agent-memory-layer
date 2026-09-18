"""技能作用域（scope）：global / project:<名>，每个作用域自由布置多个技能目录。

用户要求（2026-09-18）逐条对应到这里：
  · **不同项目自由布置 skills** —— 项目作用域里的目录写成**相对项目根**的路径，爱放哪放哪
  · **项目内可以改技能** —— 见 `install.py` 的 fork / overlay（改了不算"脏"，算"有意的分叉"）
  · **可选要不要同步知识库** —— 每个目录一个 `sync_kb`；默认：全局目录同步、项目目录**不同步**
  · **项目内技能可以单独存放** —— 项目作用域的技能就住在项目里，知识库只留"状态"（见 `versions.py`）

配置（`patrol.scopes`）。**没配就按老的 `skill_roots` 派生一个 global 作用域** —— 老配置不用改：

```yaml
patrol:
  scopes:
    - name: global
      kind: global
      roots:
        - {name: agents-skills, path: "~/.agents/skills", sync_kb: true, mirror_to: 技能原始/skills-shared}
        - {name: claude-code-skills, path: "~/.claude/skills", sync_kb: false}
      sources: [mattpocock/skills]
    - name: project:demo
      kind: project
      path: "C:/work/demo"
      roots:
        - {name: project-skills, path: ".agents/skills", sync_kb: false}
```

设计取舍：
  · 作用域名 `project:<名>` 只是**约定**，不强求；`kind: project` + `path` 才是判定依据
  · 技能名冲突时"先到先得"（配置顺序即优先级），跨作用域同名技能**不合并**：
    它们是两份独立的东西，各自有自己的版本历史（否则项目分叉会被全局更新冲掉）
"""
from __future__ import annotations

import os

from ..config import expand


def _root_entry(scope_name: str, kind: str, entry: dict, base: str | None) -> dict:
    raw = str(entry.get("path") or "")
    path = expand(raw)
    if path and not os.path.isabs(path) and base:
        path = os.path.join(expand(base), path)
    name = entry.get("name") or (os.path.basename(path.rstrip("/\\")) if path else "skills")
    # 项目里的技能默认**不进知识库**（用户明确要"单独存放在项目里"），要同步就显式写 sync_kb: true
    default_sync = kind != "project"
    return {
        "scope": scope_name,
        "kind": kind,
        "name": name,
        "path": path,
        "sync_kb": bool(entry.get("sync_kb", default_sync)),
        "mirror_to": entry.get("mirror_to") or (f"技能原始/{name}" if
                                                entry.get("sync_kb", default_sync) else ""),
    }


def derive_global_scope(cfg) -> dict:
    """老配置（只有 `skill_roots`）派生出的 global 作用域 —— 保证向后兼容。"""
    patrol = cfg.section("patrol")
    roots = []
    for entry in patrol.get("skill_roots") or []:
        item = dict(entry)
        item.setdefault("sync_kb", True)      # 老行为：skill_roots 都是要镜像的
        roots.append(item)
    return {"name": "global", "kind": "global", "path": None, "roots": roots,
            # 注意：老配置的 `candidate_repos` **不塞进 scopes.sources**。
            # 它是"没来源时兜底找上游"的旧机制，仍然由 `sources.seed_sources` 读；
            # 混进 scopes 会让"来源文件是唯一真相"这条规则出现两个入口（测试抓过）。
            "sources": [], "derived": True}


def load_scopes(cfg) -> list:
    """读作用域定义（没有 `scopes` 就派生一个 global）。返回原始结构 + 派生字段。"""
    raw = cfg.section("patrol").get("scopes")
    if not raw:
        return [derive_global_scope(cfg)]
    out = []
    for index, entry in enumerate(raw):
        kind = str(entry.get("kind") or ("project" if entry.get("path") else "global"))
        name = str(entry.get("name") or (f"project:{os.path.basename(str(entry.get('path')))}"
                                        if kind == "project" else f"scope{index}"))
        out.append({"name": name, "kind": kind, "path": expand(str(entry.get("path") or "")) or None,
                    "roots": list(entry.get("roots") or []),
                    "sources": list(entry.get("sources") or []),
                    "derived": False})
    return out


def all_roots(cfg) -> list:
    """所有作用域下的所有技能目录（**治理**视角：不管同不同步知识库）。"""
    out = []
    for scope in load_scopes(cfg):
        for entry in scope["roots"]:
            root = _root_entry(scope["name"], scope["kind"], entry, scope.get("path"))
            root["exists"] = bool(root["path"]) and os.path.isdir(root["path"])
            out.append(root)
    return out


def kb_roots(cfg) -> list:
    """要同步进知识库的技能目录（`sync_kb: true` 且目录存在）。"""
    return [r for r in all_roots(cfg) if r["sync_kb"] and r["mirror_to"] and r["exists"]]


def names(cfg) -> list:
    return [s["name"] for s in load_scopes(cfg)]


def get(cfg, name: str) -> dict:
    for scope in load_scopes(cfg):
        if scope["name"] == name:
            return scope
    raise KeyError(f"没有这个作用域：{name}（现有：{', '.join(names(cfg))}）")


def resolve(cfg, name: str | None = None, project: str | None = None) -> dict:
    """定位作用域：给名字最直接；给项目路径就找最长的前缀匹配。"""
    if name:
        return get(cfg, name)
    if project:
        found = for_path(cfg, project)
        if found:
            return found
        raise KeyError(f"没有作用域覆盖这个项目：{project}（用 `aml patrol scopes` 看现有作用域）")
    scopes = load_scopes(cfg)
    for scope in scopes:
        if scope["kind"] == "global":
            return scope
    return scopes[0]


def for_path(cfg, path: str) -> dict | None:
    """哪个项目作用域管这个路径（最长前缀优先，避免父子项目抢）。"""
    target = os.path.abspath(expand(str(path)))
    best, best_len = None, -1
    for scope in load_scopes(cfg):
        base = scope.get("path")
        if scope["kind"] != "project" or not base:
            continue
        base_abs = os.path.abspath(expand(base))
        if target == base_abs or target.startswith(base_abs + os.sep):
            if len(base_abs) > best_len:
                best, best_len = scope, len(base_abs)
    return best


def of_skill(cfg, skill_name: str) -> dict | None:
    """技能住在哪个作用域（同名多处时按配置顺序先到先得，与 `skills.all_skills` 同口径）。"""
    for root in all_roots(cfg):
        candidate = os.path.join(root["path"], skill_name)
        if os.path.isfile(os.path.join(candidate, "SKILL.md")):
            return root
    return None


def default_root(cfg, scope: dict) -> dict | None:
    """往哪个目录装：该作用域第一个存在的根；都不存在就用第一个（会建）。"""
    roots = [r for r in all_roots(cfg) if r["scope"] == scope["name"]]
    for root in roots:
        if root["exists"]:
            return root
    return roots[0] if roots else None


def summary(cfg) -> list:
    """给 CLI 看的一行一作用域：技能数、目录、是否同步知识库。"""
    from . import skills as skills_mod
    rows = []
    for root in all_roots(cfg):
        found = skills_mod.discover_skills(root["path"]) if root["exists"] else []
        rows.append({**root, "skills": len(found)})
    return rows


def render(cfg) -> str:
    rows = summary(cfg)
    if not rows:
        return "没有配置任何技能目录（`patrol.scopes` 或 `patrol.skill_roots`）"
    lines = ["技能作用域："]
    current = None
    for row in rows:
        if row["scope"] != current:
            current = row["scope"]
            scope = next((s for s in load_scopes(cfg) if s["name"] == current), {})
            mark = "（派生自 skill_roots，未显式配置）" if scope.get("derived") else ""
            where = f"  项目根：{scope.get('path')}" if scope.get("kind") == "project" else ""
            lines.append(f"  [{row['kind']}] {current}{mark}{where}")
        state = "已同步知识库" if row["sync_kb"] else "只在本地"
        lines.append(f"      {row['name']:<22} {row['skills']:>3} 个技能  {state}"
                     f"{'' if row['exists'] else '  （目录不存在）'}")
        lines.append(f"          {row['path']}")
    return "\n".join(lines)


__all__ = ["derive_global_scope", "load_scopes", "all_roots", "kb_roots", "names", "get",
           "resolve", "for_path", "of_skill", "default_root", "summary", "render"]
