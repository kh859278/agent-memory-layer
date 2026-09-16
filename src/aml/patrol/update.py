"""技能上游纳管与更新（监控 + 自动更新 + 暂存复核）。

自动更新的**三条安全闸门**（这是整个模块存在的理由：别把用户的本地改动冲掉）：

  1. `local_patch` 有值 —— 明确标注过"这个技能我改过"，**永不覆盖**，只暂存 + 通知
  2. `local_diff: true`，或本地指纹 ≠ 元数据里的 `content_hash` —— 本地被动过，只暂存
  3. 其余（本地干净）—— 备份 → 覆盖 → 更新元数据

判断"上游变没变"优先用 sha（便宜），拿不到 sha 时可以退到内容指纹（`deep=True`，慢）。
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import github, skills

ACTIONS = ("updated", "staged", "patched", "uptodate", "failed", "unknown")


def _blank():
    return {key: [] for key in ACTIONS}


# ------------------------------------------------------------------ 纳管

def adopt(cfg, log=print, dry_run: bool = False, apply_mode: str = "none",
          repos: list | None = None, only: set | None = None) -> dict:
    """给"没有元数据"的技能补上游来源。

    apply_mode：none（只暂存，默认）/ no-local-only（本地没有独有文件才覆盖）/ all。
    """
    patrol = cfg.section("patrol")
    repos = repos or patrol.get("candidate_repos") or []
    min_hits = int(patrol.get("min_repo_hits", 3))
    untracked = [(name, path) for name, path, _ in skills.all_skills(cfg)
                 if not skills.read_meta(path)[0] and (not only or name in only)]
    result = {"untracked": len(untracked), "claims": {}, "clean": [], "applied": [],
              "dirty": [], "failed": []}
    if not untracked:
        log("没有需要纳管的技能（都有上游来源），跳过下载")
        return result

    names = {name for name, _ in untracked}
    claims, snapshots = {}, {}
    for repo in repos:
        if not names - set(claims):
            break
        try:
            root, branch, scratch = github.fetch_repo(cfg, repo)
        except Exception as e:  # noqa: BLE001
            log(f"  跳过 {repo}：{type(e).__name__} {str(e)[:80]}")
            continue
        snapshots[repo] = (root, branch, scratch)
        remote = skills.remote_skill_dirs(root)
        hits = sorted((names - set(claims)) & set(remote))
        log(f"  {repo}@{branch}：远端 {len(remote)} 个技能，命中本地未跟踪 {len(hits)} 个")
        if len(hits) < min_hits:
            continue
        try:
            sha, via = github.remote_sha(repo, branch)
            log(f"    HEAD {sha[:7]}（via {via}）")
        except Exception as e:  # noqa: BLE001
            sha = None
            log(f"    取远端 sha 失败（{str(e)[:60]}），改记内容指纹")
        for name in hits:
            claims[name] = (repo, remote[name], branch, sha)

    result["claims"] = {name: f"{repo}/{subdir}" for name, (repo, subdir, _, _) in claims.items()}
    for name, (repo, subdir, branch, sha) in sorted(claims.items()):
        local_dir = dict(untracked)[name]
        up_dir = os.path.join(snapshots[repo][0], subdir)
        try:
            cmp = skills.compare_dirs(local_dir, up_dir)
            same = not (cmp["only_a"] or cmp["only_b"] or cmp["differ"])
            meta = {"name": name, "repo": repo, "subdir": subdir, "branch": branch, "commit": sha,
                    "adopted_at": skills.today(), "last_checked": skills.today(),
                    "content_hash": skills.dir_hash(local_dir),
                    "upstream_hash": skills.dir_hash(up_dir), "local_diff": not same}
            if same:
                result["clean"].append(name)
            elif apply_mode == "all" or (apply_mode == "no-local-only" and not cmp["only_a"]):
                result["applied"].append(name)
                if not dry_run:
                    skills.backup_skill(cfg, local_dir, name, "pre-adopt")
                    skills.sync_dir(up_dir, local_dir)
                    meta["content_hash"] = skills.dir_hash(local_dir)
                    meta["upstream_hash"] = meta["content_hash"]
                    meta["local_diff"] = False
            else:
                result["dirty"].append(name)
                if not dry_run:
                    staged = skills.stage_skill(cfg, up_dir, name, meta["upstream_hash"][:7])
                    meta["staged_upstream"] = staged
            if not dry_run:
                skills.write_meta(local_dir, meta)
        except Exception as e:  # noqa: BLE001
            result["failed"].append(name)
            log(f"  ❌ {name}：{type(e).__name__} {str(e)[:80]}")
    for _, _, scratch in snapshots.values():
        shutil.rmtree(scratch, ignore_errors=True)
    log(f"纳管完成：一致 {len(result['clean'])}，已覆盖 {len(result['applied'])}，"
        f"待复核 {len(result['dirty'])}，失败 {len(result['failed'])}")
    return result


# ------------------------------------------------------------------ 更新

def group_by_repo(cfg) -> tuple:
    """把受跟踪技能按 (repo, branch) 分组；git 安装的单独一组。"""
    groups, gits = {}, []
    for name, path, _ in skills.all_skills(cfg):
        meta, mpath = skills.read_meta(path)
        repo = (meta or {}).get("repo") or ""
        if repo and not str(repo).startswith("("):
            groups.setdefault((repo, meta.get("branch")), []).append((name, path, meta, mpath))
        elif skills.is_git_install(path):
            gits.append((name, path, meta or {}, mpath))
    return groups, gits


def update_one(cfg, name, local_dir, meta, mpath, up_dir, sha) -> tuple:
    """对"上游变了"的技能决定动作。返回 (动作, 说明)。"""
    old = meta.get("commit")
    up_hash = skills.dir_hash(up_dir)
    local_hash = skills.dir_hash(local_dir)

    if meta.get("local_patch"):
        staged = skills.stage_skill(cfg, up_dir, name, (sha or up_hash)[:7])
        meta.update({"local_diff": True, "last_checked": skills.today(),
                     "upstream_hash": up_hash, "staged_upstream": staged})
        if sha:
            meta["commit"] = sha
        if mpath:
            skills.write_meta(local_dir, meta, mpath)
        return "patched", "有本地补丁，上游新版只暂存"
    if meta.get("local_diff") or (meta.get("content_hash") and local_hash != meta["content_hash"]):
        staged = skills.stage_skill(cfg, up_dir, name, (sha or up_hash)[:7])
        meta.update({"local_diff": True, "last_checked": skills.today(),
                     "upstream_hash": up_hash, "staged_upstream": staged})
        if sha:
            meta["commit"] = sha
        if mpath:
            skills.write_meta(local_dir, meta, mpath)
        return "staged", "本地有改动，上游新版只暂存"
    if meta.get("upstream_hash") and up_hash == meta["upstream_hash"]:
        if sha:
            meta["commit"] = sha
        meta["last_checked"] = skills.today()
        if mpath:
            skills.write_meta(local_dir, meta, mpath)
        return "uptodate", "上游内容无变化（仅提交变化）"

    skills.backup_skill(cfg, local_dir, name, (old or "unknown")[:7])
    skills.sync_dir(up_dir, local_dir)
    meta.update({"content_hash": skills.dir_hash(local_dir), "upstream_hash": up_hash,
                 "local_diff": False, "installed_at": skills.today(), "last_checked": skills.today()})
    if sha:
        meta["commit"] = sha
    meta.pop("staged_upstream", None)
    skills.write_meta(local_dir, meta, mpath)
    return "updated", f"已更新 {(old or '?')[:7]} → {(sha or '内容')[:7] if sha else '（按内容）'}"


def check(cfg, log=print, check_only: bool = False, deep: bool = False) -> tuple:
    """探测 + 需要时更新。返回 (结果字典, 明细行)。

    有**总时间预算**（`patrol.update.detect_budget_sec`，默认 120 秒）：
    github 抖动时 git 会一个个挂到超时，不加预算的话一轮能被拖到几分钟（实测踩过）。
    超预算后剩余仓库记「本轮未判断」，下轮再试——定时任务宁可少查一轮。
    """
    import time
    update_cfg = cfg.section("patrol").get("update", {}) or {}
    budget = float(update_cfg.get("detect_budget_sec", 120) or 120)
    git_timeout = int(update_cfg.get("git_timeout_sec", 15) or 15)
    started = time.time()
    groups, gits = group_by_repo(cfg)
    results, details = _blank(), []
    for (repo, branch), items in sorted(groups.items(), key=lambda kv: str(kv[0])):
        if time.time() - started > budget:
            log(f"  ⏱ 探测已用 {time.time() - started:.0f}s，超预算 {budget:.0f}s，"
                f"剩余仓库本轮不判断（下轮重试）")
            for name, *_ in items:
                results["unknown"].append(name)
                details.append(f"- ⚠️ **{name}**：探测超预算，本轮未判断")
            continue
        sha, via = None, "-"
        try:
            sha, via = github.remote_sha(repo, branch, git_timeout=git_timeout)
        except Exception as e:  # noqa: BLE001
            log(f"  {repo}@{branch} 远端 sha 取不到：{str(e)[:70]}")

        if not sha and not deep:
            log(f"  {repo}@{branch}：网络抖了，本轮不判断（下轮重试）")
            for name, *_ in items:
                results["unknown"].append(name)
                details.append(f"- ⚠️ **{name}**：拿不到远端 sha，本轮未判断")
            continue

        if sha:
            todo = [(n, d, m, p) for (n, d, m, p) in items if (m.get("commit") or "") != sha]
            log(f"  {repo}@{branch} HEAD {sha[:7]}（via {via}）：{len(items)} 个技能，{len(todo)} 个有新提交")
            if not todo:
                if not check_only:
                    for _name, d, meta, mpath in items:
                        meta["last_checked"] = skills.today()
                        if mpath:
                            skills.write_meta(d, meta, mpath)
                results["uptodate"] += [n for n, *_ in items]
                details += [f"- ✅ **{n}**：已是最新（{sha[:7]}）" for n, *_ in items]
                continue
        else:
            todo = items
            if check_only:
                for name, *_ in items:
                    results["unknown"].append(name)
                    details.append(f"- ⚠️ **{name}**：拿不到 sha，--check-only 无法判断")
                continue

        if check_only:
            for name, _d, meta, _mpath in todo:
                results["staged"].append(name)
                details.append(f"- 🆕 **{name}**：有新提交 {(meta.get('commit') or '?')[:7]} → "
                               f"{(sha or '(未知)')[:7]}（仅检查，未动手）")
            continue

        try:
            root, used, scratch = github.fetch_repo(cfg, repo, branch)
        except Exception as e:  # noqa: BLE001
            log(f"  下载 {repo} 快照失败：{type(e).__name__} {str(e)[:80]}（下轮再试）")
            for name, *_ in todo:
                results["failed"].append(name)
                details.append(f"- ❌ **{name}**：有新版但快照下载失败，下轮再试")
            continue
        try:
            for name, local_dir, meta, mpath in todo:
                subdir = meta.get("subdir") or ""
                up_dir = os.path.join(root, subdir) if subdir else root
                if not os.path.isdir(up_dir):
                    results["failed"].append(name)
                    details.append(f"- ❌ **{name}**：上游目录 `{subdir}` 不存在")
                    continue
                if sha is None:      # 二级判断：内容指纹没变就不出手
                    up_hash = skills.dir_hash(up_dir)
                    if meta.get("upstream_hash") and up_hash == meta["upstream_hash"]:
                        meta["last_checked"] = skills.today()
                        if mpath:
                            skills.write_meta(local_dir, meta, mpath)
                        results["uptodate"].append(name)
                        details.append(f"- ✅ **{name}**：内容指纹未变（sha 取不到，用指纹判断）")
                        continue
                try:
                    action, why = update_one(cfg, name, local_dir, meta, mpath, up_dir, sha)
                except Exception as e:  # noqa: BLE001
                    action, why = "failed", f"{type(e).__name__} {str(e)[:80]}"
                results[action].append(name)
                icon = {"updated": "⬆️", "staged": "📥", "patched": "🛡️", "uptodate": "✅",
                        "failed": "❌", "unknown": "⚠️"}[action]
                details.append(f"- {icon} **{name}**（{repo}@{used}）：{why}")
                log(f"  {icon} {name}: {why}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # git 安装的：只尝试快进拉取（有本地提交会自动失败，是安全的）
    for name, local_dir, _meta, _mpath in gits:
        if check_only:
            details.append(f"- ℹ️ **{name}**（git 安装）：仅检查模式跳过")
            continue
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
        try:
            r = subprocess.run(["git", "-c", "http.sslBackend=openssl", "-C", local_dir,
                                "pull", "--ff-only"], capture_output=True, text=True,
                               timeout=180, env=env)
            if r.returncode == 0:
                results["updated"].append(name)
                details.append(f"- ⬆️ **{name}**（git）：已快进")
            else:
                results["uptodate"].append(name)
                details.append(f"- ℹ️ **{name}**（git）：无需更新或拉不动")
        except Exception as e:  # noqa: BLE001
            results["uptodate"].append(name)
            details.append(f"- ℹ️ **{name}**（git）：{type(e).__name__}")
    return results, details


def accept(cfg, name: str | None = None, all_: bool = False, log=print) -> dict:
    """采纳暂存的上游版本（覆盖本地，先备份）。"""
    items = skills.pending_items(cfg)
    targets = [x for x in items if all_ or (name and x["name"] == name)]
    done = []
    for item in targets:
        local = next((path for n, path, _ in skills.all_skills(cfg) if n == item["name"]), None)
        if not local:
            log(f"  跳过 {item['name']}：本地没有这个技能")
            continue
        meta, mpath = skills.read_meta(local)
        meta = meta or {"name": item["name"]}
        skills.backup_skill(cfg, local, item["name"], "pre-accept")
        skills.sync_dir(item["path"], local)
        meta.update({"content_hash": skills.dir_hash(local), "local_diff": False,
                     "installed_at": skills.today(), "last_checked": skills.today()})
        meta["upstream_hash"] = meta["content_hash"]
        meta.pop("staged_upstream", None)
        skills.write_meta(local, meta, mpath)
        shutil.rmtree(item["path"], ignore_errors=True)
        done.append(item["name"])
        log(f"  ✅ {item['name']} 已采纳上游版（旧版已备份）")
    return {"accepted": done, "requested": len(targets)}
