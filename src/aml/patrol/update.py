"""技能上游纳管与更新（监控 + 自动更新 + 暂存复核）。

自动更新的**三条安全闸门**（这是整个模块存在的理由：别把用户的本地改动冲掉）：

  1. `local_patch` 有值 —— 明确标注过"这个技能我改过"，**永不覆盖**，只暂存 + 通知
  2. `local_diff: true`，或本地指纹 ≠ 元数据里的 `content_hash` —— 本地被动过，只暂存
  3. 其余（本地干净）—— 备份 → 覆盖 → 更新元数据

**第 4 条闸门（能力与生命周期，2026-09-17 加；2026-09-22 收紧）**：本地干净也**不一定**自动覆盖 ——
还要看 `patrol.capability.gate`：技能生命周期状态必须是 `approved`/`active`、
**而且有批准凭据**（`baseline_hash`，即"人批准过这一版内容"）、
`skill.yaml` 没声明 `requires_approval`、上游新版没有"新增的未声明高风险能力"。
不满足就只暂存（动作记为 `gated`）并说明原因。

为什么 2026-09-22 要加"批准凭据"这一条：原来状态本身就够了，而 `infer_state()`
会把"受跟踪 + 本地干净"推断成 `active` —— 于是**从没被人看过**的技能和**人批准过**的技能
待遇完全一样，自动覆盖在没有任何人参与的情况下就发生了（本机实测 26/38 个技能处于该状态）。
这条闸门要解决的正是"一个从没被人看过的新技能和用了半年的老技能待遇一样"这个问题。

判断"上游变没变"优先用 sha（便宜），拿不到 sha 时可以退到内容指纹（`deep=True`，慢）。
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import capability, github, skills

ACTIONS = ("updated", "staged", "patched", "uptodate", "failed", "unknown", "gated")


def _blank():
    return {key: [] for key in ACTIONS}


# ------------------------------------------------------------------ 纳管

def adopt(cfg, log=print, dry_run: bool = False, apply_mode: str = "none",
          repos: list | None = None, only: set | None = None) -> dict:
    """给"没有元数据"的技能补上游来源。

    上游来源**以 `sources` 为准**（`patrol sources add`，带 layout/子目录/优先级），
    匹配方式是**归一化后的精确同名**（`Skill-Name` == `skill_name`），
    不再用"目录名命中 ≥3 个就认这个仓库"那种猜法 —— 猜错的代价是拿别人的内容覆盖你的文件。

    老配置（只写了 `candidate_repos`）仍然能用：没有来源时退回那份清单，layout 按 auto 认。

    apply_mode：none（只暂存，默认）/ no-local-only（本地没有独有文件才覆盖）/ all。
    """
    from . import sources as sources_mod
    patrol = cfg.section("patrol")
    if repos:
        source_list = [sources_mod.get(cfg, r) or {"repo": r, "layout": "auto"} for r in repos]
    else:
        source_list = sources_mod.enabled_sources(cfg)
    legacy = False
    if not source_list:
        legacy = True
        source_list = [{"repo": r, "layout": "auto"}
                       for r in patrol.get("candidate_repos") or []]
    min_hits = int(patrol.get("min_repo_hits", 3))
    untracked = [(name, path) for name, path, _ in skills.all_skills(cfg)
                 if not skills.read_meta(path)[0] and (not only or name in only)]
    result = {"untracked": len(untracked), "claims": {}, "clean": [], "applied": [],
              "dirty": [], "failed": [], "layout": {}, "legacy_matching": legacy}
    if not untracked:
        log("没有需要纳管的技能（都有上游来源），跳过下载")
        return result

    names = {name for name, _ in untracked}
    claims, snapshots = {}, {}
    for source in source_list:
        repo = source.get("repo")
        if not repo or not names - set(claims):
            break
        try:
            root, branch, scratch = github.fetch_repo(cfg, repo, source.get("branch"))
        except Exception as e:  # noqa: BLE001
            log(f"  跳过 {repo}：{type(e).__name__} {str(e)[:80]}")
            continue
        snapshots[repo] = (root, branch, scratch)
        detected = sources_mod.detect_layouts(root)
        remote = sources_mod.enumerate_source(source, root, detected)
        matched = sources_mod.match_plan(sorted(names - set(claims)), remote)
        log(f"  {repo}@{branch}：远端 {len(remote)} 个技能"
            f"（布局 {detected['layouts'] or '-'}），按名字精确命中本地未跟踪 {len(matched)} 个")
        if legacy and len(matched) < min_hits:
            log(f"    命中数 < min_repo_hits({min_hits})，老规矩：不认这个仓库")
            continue
        if not matched:
            continue
        try:
            sha, via = github.remote_sha(repo, source.get("branch"))
            log(f"    HEAD {sha[:7]}（via {via}）")
        except Exception as e:  # noqa: BLE001
            sha = None
            log(f"    取远端 sha 失败（{str(e)[:60]}），改记内容指纹")
        for name, subdir in matched.items():
            claims[name] = (repo, subdir, branch, sha)
            result["layout"][name] = sources_mod.classify(subdir)

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

    # 第 4 条闸门：本地干净也要过能力/生命周期审批（见模块 docstring）
    verdict = capability.gate(cfg, name, local_dir, up_dir, meta=meta)
    if not verdict["allow"]:
        staged = skills.stage_skill(cfg, up_dir, name, (sha or up_hash)[:7])
        meta.update({"last_checked": skills.today(), "upstream_hash": up_hash,
                     "staged_upstream": staged, "staged_reason": "；".join(verdict["reasons"])})
        if sha:
            meta["commit"] = sha
        if mpath:
            skills.write_meta(local_dir, meta, mpath)
        return "gated", "只暂存待批：" + "；".join(verdict["reasons"])

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
                        "failed": "❌", "unknown": "⚠️", "gated": "🔒"}[action]
                details.append(f"- {icon} **{name}**（{repo}@{used}）：{why}")
                log(f"  {icon} {name}: {why}")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # git 安装的：只尝试快进拉取（有本地提交会自动失败，是安全的）
    pull_timeout = int(update_cfg.get("git_pull_timeout_sec", 60) or 60)

    def head_of(path):
        try:
            return subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True,
                                  text=True, timeout=20).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    for name, local_dir, _meta, _mpath in gits:
        if check_only:
            details.append(f"- ℹ️ **{name}**（git 安装）：仅检查模式跳过")
            continue
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
        before = head_of(local_dir)
        try:
            r = subprocess.run(["git", "-c", "http.sslBackend=openssl", "-C", local_dir,
                                "pull", "--ff-only"], capture_output=True, text=True,
                               timeout=pull_timeout, env=env)
            after = head_of(local_dir)
            # 注意：`git pull` 在"Already up to date."时也返回 0 —— 只看返回码会把没变化
            # 误报成"已更新"（每轮假播报，实测踩过）。必须比 HEAD 有没有变。
            if r.returncode == 0 and before and after and before != after:
                results["updated"].append(name)
                details.append(f"- ⬆️ **{name}**（git）：已快进 {before[:7]} → {after[:7]}")
            else:
                results["uptodate"].append(name)
                details.append(f"- ✅ **{name}**（git）：已是最新"
                               if r.returncode == 0 else f"- ℹ️ **{name}**（git）：拉不动")
        except subprocess.TimeoutExpired:
            results["unknown"].append(name)
            details.append(f"- ⚠️ **{name}**（git）：拉取超 {pull_timeout}s，本轮跳过（下轮重试）")
        except Exception as e:  # noqa: BLE001
            results["uptodate"].append(name)
            details.append(f"- ℹ️ **{name}**（git）：{type(e).__name__}")
    return results, details


def accept(cfg, name: str | None = None, all_: bool = False, log=print,
           allow_risky: bool = False, require_review: bool = True) -> dict:
    """采纳暂存的上游版本（覆盖本地，先备份）。

    **人就是那道批准**，但"批准"从 2026-09-21 起要带证据：

      1. **审的与采纳的必须是同一版**：`patrol diff` 会记下暂存内容指纹（`reviewed.json`），
         这里核对；对不上就跳过 —— 否则会出现"审的是 A、批准的是 B"
      2. **摆出能力差异**：新增未声明的高危能力 / 声明了 requires_approval → 必须 `--yes` 显式确认
      3. **批准绑定内容 hash**：采纳后写 `baseline_hash` 与能力快照（`capability_history` 取并集），
         本地内容之后一变，批准自动作废，敏感能力"删掉又回来"也会被要求重新批准
    """
    items = skills.pending_items(cfg)
    targets = [x for x in items if all_ or (name and x["name"] == name)]
    done, skipped, risky = [], {}, {}
    for item in targets:
        skill_name = item["name"]
        local = next((path for n, path, _ in skills.all_skills(cfg) if n == skill_name), None)
        if not local:
            skipped[skill_name] = "本地没有这个技能"
            log(f"  跳过 {skill_name}：本地没有这个技能")
            continue
        staged = item["path"]
        expected = capability.reviewed_hash(cfg, skill_name)
        if require_review and expected and expected != skills.dir_hash(staged):
            skipped[skill_name] = "审阅之后暂存内容又变了（审的是 A、现在是 B）：重跑 aml patrol diff 再审"
            log(f"  ⚠ {skill_name}：{skipped[skill_name]}")
            continue
        risk = capability.accept_risk(cfg, skill_name, local, staged)
        if risk["reasons"] and not allow_risky:
            risky[skill_name] = risk["reasons"]
            skipped[skill_name] = "有风险项，需 --yes 确认：" + "；".join(risk["reasons"])
            log(f"  🔒 {skill_name}：{skipped[skill_name]}")
            for line in risk["lines"]:
                log(f"      {line}")
            continue
        for line in risk["lines"]:
            log(f"      {line}")
        meta, mpath = skills.read_meta(local)
        meta = meta or {"name": skill_name}
        skills.backup_skill(cfg, local, skill_name, "pre-accept")
        skills.sync_dir(staged, local)
        meta.update({"content_hash": skills.dir_hash(local), "local_diff": False,
                     "installed_at": skills.today(), "last_checked": skills.today()})
        meta["upstream_hash"] = meta["content_hash"]
        meta.pop("staged_upstream", None)
        meta.pop("staged_reason", None)
        skills.write_meta(local, meta, mpath)
        shutil.rmtree(staged, ignore_errors=True)
        state = capability.state_of(cfg, skill_name)
        if state in capability.BLOCKED:
            # 停用/退役的技能：人明确要求也照做，但状态不动（留着"它本该停用"的痕迹）
            log(f"  ⚠ {skill_name} 生命周期为 {state}，仍按你的要求采纳了（状态未改）")
        else:
            capability.record_approval(cfg, skill_name, local, meta=meta, by="human",
                                       why="人工采纳上游版本",
                                       log=lambda *_a, **_k: None)
        done.append(skill_name)
        log(f"  ✅ {skill_name} 已采纳上游版（旧版已备份；批准绑定内容 "
            f"{meta['content_hash'][:7]}）")
    return {"accepted": done, "requested": len(targets), "skipped": skipped, "risky": risky}
