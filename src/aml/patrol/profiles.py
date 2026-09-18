"""profile：把"要哪些技能 + 装到哪个作用域 + 从哪来"记成一份**可复现清单**，并能推到远端/从远端拉。

对标 SkillTruck 的 profile + `config push/pull`。**关键差别**：它是交互式选，
我们的定时任务是 09:30 跑的、必须非交互 —— 所以 profile 就是一份 JSON，
`aml patrol install --profile laptop` 一条命令复现整套技能集。

profile 长这样（`state/patrol/profiles.json`）：

```json
{"profiles": {"laptop": {"scope": "global", "note": "笔记本上要的",
                         "skills": {"tdd": "mattpocock/skills",
                                    "grilling": "mattpocock/skills"}}}}
```

远端两种传输（`patrol.config.remote` 或命令行给）：
  · **一个路径**：写到/读自那个文件（放在你自己的同步目录/仓库里最省事）
  · **一个 git 仓库 URL**：浅克隆到临时目录 → 写 `aml-profiles.json` → commit → push；
    拉取就是浅克隆后读。SSH 地址走本机既有凭证，HTTPS 受主机白名单约束。

合并口径（拉取时）：远端有、本地没有 → 直接加；两边都有同名 profile → **不覆盖**，
报成冲突让人决定（profile 里装着"要装哪些技能"，静默覆盖等于偷偷改别人的机器）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess

from . import github, scopes

PROFILE_FILE = "aml-profiles.json"


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_path(cfg):
    return cfg.state_dir / "patrol" / "profiles.json"


def load(cfg) -> dict:
    try:
        with open(default_path(cfg), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"profiles": {}}
    data.setdefault("profiles", {})
    return data


def save(cfg, data: dict) -> str:
    path = default_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return str(path)


def put(cfg, name: str, scope: str, skills_map: dict, note: str = "", log=print) -> dict:
    """新建/覆盖一个 profile。`skills_map` 是 {技能名: 来源 key 或 repo}。"""
    data = load(cfg)
    profile = {"scope": scope, "note": note, "skills": dict(skills_map), "updated_at": _now()}
    data["profiles"][name] = profile
    save(cfg, data)
    log(f"  profile {name}：{len(skills_map)} 个技能 → 作用域 {scope}")
    return profile


def get(cfg, name: str) -> dict:
    profile = (load(cfg).get("profiles") or {}).get(name)
    if not profile:
        raise KeyError(f"没有这个 profile：{name}（现有："
                       f"{', '.join(sorted(load(cfg).get('profiles') or {})) or '无'}）")
    return profile


def remove(cfg, name: str, log=print) -> bool:
    data = load(cfg)
    if name not in (data.get("profiles") or {}):
        return False
    data["profiles"].pop(name)
    save(cfg, data)
    log(f"  profile 已删除：{name}（已装的技能不动）")
    return True


def groups(profile: dict) -> dict:
    """按来源分组：{来源: [技能...]} —— 一次装一组，少下载几遍仓库。"""
    out = {}
    for skill, source in (profile.get("skills") or {}).items():
        out.setdefault(source or "", []).append(skill)
    return out


def snapshot(cfg, name: str, scope_name: str | None = None,
             project: str | None = None, note: str = "", log=print) -> dict:
    """用"当前已装的技能"反向生成 profile（在装好的机器上 `profile save` 最省事）。"""
    from . import lockfile
    scope = scopes.resolve(cfg, scope_name, project)
    skills_map = {}
    for row in lockfile.status_rows(cfg):
        if row["scope"] != scope["name"] or not row["tracked"]:
            continue
        skills_map[row["name"]] = row["repo"]
    return put(cfg, name, scope["name"], skills_map, note=note, log=log)


def render(cfg) -> str:
    profiles = load(cfg).get("profiles") or {}
    if not profiles:
        return "还没有 profile（`aml patrol profile save <名> --scope global` 从当前已装技能生成）"
    lines = [f"profile（{len(profiles)} 个）："]
    for name, profile in sorted(profiles.items()):
        lines.append(f"  {name:<20} 作用域 {profile.get('scope')}　"
                     f"{len(profile.get('skills') or {})} 个技能"
                     + (f"　（{profile.get('note')}）" if profile.get("note") else ""))
        for source, items in sorted(groups(profile).items()):
            lines.append(f"      ← {source or '(未指定来源)'}：{', '.join(sorted(items))}")
    lines.append("  复现：`aml patrol install --profile <名>`")
    return "\n".join(lines)


# ------------------------------------------------------------------ 远端

def is_git_remote(remote: str) -> bool:
    remote = str(remote or "")
    if remote.startswith("file://"):
        return False
    return (remote.startswith(("git@", "ssh://", "https://", "http://"))
            and (remote.endswith(".git") or "://" in remote or remote.startswith("git@")))


def _git(args, cwd=None, timeout: int = 120, run=None) -> tuple:
    if run is not None:
        return run(args, cwd)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
    proc = subprocess.run(["git", "-c", "http.sslBackend=openssl"] + args, cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def export(cfg) -> dict:
    return {"version": 1, "exported_at": _now(), "profiles": load(cfg).get("profiles") or {}}


def merge(cfg, remote_data: dict, log=print) -> dict:
    """把远端 profile 并进来：新的直接加，同名的**不覆盖**，报冲突。"""
    data = load(cfg)
    local = data.setdefault("profiles", {})
    added, same, conflicts = [], [], []
    for name, profile in (remote_data.get("profiles") or {}).items():
        if name not in local:
            local[name] = profile
            added.append(name)
        elif json.dumps(local[name], sort_keys=True, ensure_ascii=False) == \
                json.dumps(profile, sort_keys=True, ensure_ascii=False):
            same.append(name)
        else:
            conflicts.append(name)
    if added:
        save(cfg, data)
    log(f"  拉取结果：新增 {len(added)}，相同 {len(same)}，冲突 {len(conflicts)}"
        + (f"（冲突：{', '.join(conflicts)}）" if conflicts else ""))
    if conflicts:
        log("  冲突的没动 —— 要么改本地名，要么手动把远端那份贴进来（我不替你选，"
            "因为 profile 决定装哪些技能，静默覆盖等于替你改机器）")
    return {"added": added, "same": same, "conflicts": conflicts}


def push(cfg, remote: str, dry_run: bool = False, run=None, log=print) -> dict:
    """把全部 profile 推到远端（路径或 git 仓库）。"""
    data = export(cfg)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if not is_git_remote(remote):
        target = os.path.expanduser(remote)
        if dry_run:
            log(f"  · 将写入 {target}（{len(data['profiles'])} 个 profile）")
            return {"ok": True, "target": target, "dry_run": True}
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        log(f"  已写入 {target}（{len(data['profiles'])} 个 profile）")
        return {"ok": True, "target": target}

    github.check_url(remote, cfg)
    if dry_run:
        log(f"  · 将提交到 {remote} 的 {PROFILE_FILE}")
        return {"ok": True, "target": remote, "dry_run": True}
    scratch = github.scratch_dir(cfg, prefix="profiles-")
    try:
        rc, out = _git(["clone", "--depth", "1", remote, scratch], run=run)
        if rc != 0:
            return {"ok": False, "error": f"clone 失败：{out.strip()[-200:]}"}
        with open(os.path.join(scratch, PROFILE_FILE), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        _git(["add", PROFILE_FILE], cwd=scratch, run=run)
        rc, out = _git(["-c", "user.email=aml@local", "-c", "user.name=aml",
                        "commit", "-m", f"aml: profiles {_now()}"], cwd=scratch, run=run)
        if rc != 0 and "nothing to commit" not in out:
            return {"ok": False, "error": f"commit 失败：{out.strip()[-200:]}"}
        rc, out = _git(["push", "origin", "HEAD"], cwd=scratch, run=run)
        if rc != 0:
            return {"ok": False, "error": f"push 失败：{out.strip()[-200:]}"}
        log(f"  已推到 {remote}（{len(data['profiles'])} 个 profile）")
        return {"ok": True, "target": remote}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def pull(cfg, remote: str, dry_run: bool = False, run=None, log=print) -> dict:
    """从远端拉 profile 并合并（同名的报冲突，不覆盖）。"""
    if not is_git_remote(remote):
        path = os.path.expanduser(remote)
        if not os.path.isfile(path):
            return {"ok": False, "error": f"找不到文件：{path}"}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        github.check_url(remote, cfg)
        scratch = github.scratch_dir(cfg, prefix="profiles-")
        try:
            rc, out = _git(["clone", "--depth", "1", remote, scratch], run=run)
            if rc != 0:
                return {"ok": False, "error": f"clone 失败：{out.strip()[-200:]}"}
            target = os.path.join(scratch, PROFILE_FILE)
            if not os.path.isfile(target):
                return {"ok": False, "error": f"远端没有 {PROFILE_FILE}"}
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    if dry_run:
        log(f"  · 远端有 {len(data.get('profiles') or {})} 个 profile（--dry-run 不合并）")
        return {"ok": True, "dry_run": True, "remote": data}
    result = merge(cfg, data, log=log)
    result["ok"] = True
    return result


__all__ = ["PROFILE_FILE", "default_path", "load", "save", "put", "get", "remove", "groups",
           "snapshot", "render", "export", "merge", "push", "pull", "is_git_remote"]
