"""`state/` 是**运行数据，不是缓存**：清单 + 快照 + 体检。

起因（2026-09-21 实测）：`state/` 整棵在 **9/18 19:40 被删掉重建**（目录 CreationTime 为证），
于是 9/17 落在那里的东西 —— 6 份任务级基准报告、召回账本、基准任务表、`handoff/` ——
全没了。`state/` 被 `.gitignore` 忽略，所以 git 里查不到；删除也没人告警，
三天后才发现（`aml doctor` 当时完全不知道）。

所以这个模块存在的理由只有一句：**不可重建的那部分运行数据，必须能"当天发现被删"，并且有副本。**

三件事：
  1. `write_readme(cfg)` —— 在 `state/README.md` 写清"哪些不可重建、要清先备份"。
     生成在 state 里而不是仓库里，是因为**正准备删这个目录的人才会看见它**。
  2. `snapshot(cfg)` —— 把不可重建的文件复制到 `backups/state/<时间戳>/`。
     选 `backups/` 是因为它不在共享的 `state/` 里，且自带保留策略（这次它就没被删）。
  3. `findings(cfg)` —— 体检用。**只看"丢没丢"，不看"有没有"**：
     新装机器上这些文件本来就不存在，报"缺失"是噪声；只有当"以前快照里有过、现在没了"
     才报 BAD —— 那才是"被删了"的确切信号。
"""
from __future__ import annotations

import datetime as dt
import shutil

README_NAME = "README.md"
SNAPSHOT_ROOT = "state"
DEFAULT_KEEP = 10

# (相对 state/ 的路径, 说明, 期望多久更新一次（天，0 = 不定期，不做新鲜度检查）)
DURABLE = (
    ("recall-log.jsonl", "召回账本：哪些记忆在什么时候被注入过（反馈归因的依据）", 30),
    ("patrol/lifecycle.json", "技能生命周期与批准历史（谁在何时批准了什么）", 30),
    ("notices.json", "播报队列（丢了会漏播报或重复播报）", 30),
    ("patrol/sources.json", "技能来源登记（repo/scope/layout）", 0),
    ("patrol/profiles.json", "技能 profile / 配置同步状态", 0),
    ("bench-tasks.jsonl", "检索层基准任务表（人工写的，丢了要重写）", 0),
    ("bench-tasks-task.jsonl", "任务级基准任务表（同上）", 0),
)


def entries():
    """不可重建清单（相对路径 / 说明 / 新鲜度天数）。"""
    return [{"path": rel, "why": why, "stale_days": days} for rel, why, days in DURABLE]


def readme_path(cfg):
    return cfg.state_dir / README_NAME


def write_readme(cfg) -> str:
    """在 state/ 里放一份"别随手删"的说明（内容由代码生成，删了下次巡检会重建）。"""
    lines = [
        "# state/ —— 运行数据，不是缓存",
        "",
        "**这个目录里有不可重建的东西**：删了就没了（`state/` 被 `.gitignore` 忽略，git 里没有历史）。",
        "要清理之前，先跑一次：",
        "",
        "```bash",
        "aml state snapshot     # 把下面这些复制到 backups/state/<时间戳>/",
        "```",
        "",
        "## 不可重建（有副本才敢删）",
        "",
    ]
    for item in entries():
        stale = f"，期望 {item['stale_days']} 天内更新" if item["stale_days"] else ""
        lines.append(f"- `{item['path']}` —— {item['why']}{stale}")
    lines += [
        "",
        "## 背景（为什么会有这份说明）",
        "",
        "2026-09-18 19:40 整棵 `state/` 被删掉重建，9/17 的任务级基准报告、召回账本、",
        "基准任务表全部丢失；因为没有任何清单与告警，三天后才发现。",
        "`aml doctor` 现在会盯「以前快照里有、现在没了」这件事（`aml state check`）。",
        "",
        f"（由 `aml state readme` 生成于 {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）",
    ]
    path = readme_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return str(path)


def snapshot_root(cfg):
    return cfg.backups_dir / SNAPSHOT_ROOT


def snapshots(cfg) -> list:
    """已有快照目录（新→旧）。"""
    root = snapshot_root(cfg)
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()),
                  key=lambda p: p.name, reverse=True)


def snapshot(cfg, keep: int = DEFAULT_KEEP, stamp: str | None = None, log=print) -> dict:
    """把不可重建的文件复制到 `backups/state/<时间戳>/`，并按 keep 清理旧快照。"""
    stamp = stamp or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    keep = max(1, int(keep))       # keep=0 会把自己刚打的那份也清掉，别允许
    target = snapshot_root(cfg) / stamp
    copied, missing = [], []
    for item in entries():
        src = cfg.state_dir / item["path"]
        if not src.is_file():
            missing.append(item["path"])
            continue
        dst = target / item["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(item["path"])
    if copied:
        log(f"  运行数据快照：{len(copied)} 个文件 → {target}")
    else:
        log("  运行数据快照：没有可备份的文件（新装机器？）")
    pruned = []
    for old in snapshots(cfg)[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        pruned.append(old.name)
    if pruned:
        log(f"  清理旧快照 {len(pruned)} 个（保留最近 {keep} 个）")
    return {"dir": str(target) if copied else None, "copied": copied,
            "missing": missing, "pruned": pruned}


def _ever_snapshotted(cfg) -> set:
    """历史快照里出现过的相对路径（用来区分"从来没生成过"与"被删了"）。"""
    seen = set()
    for snap in snapshots(cfg):
        for item in entries():
            rel = item["path"]
            # 快照目录里也就是这些固定路径，直接看有没有
            if (snap / rel).is_file():
                seen.add(rel)
    return seen


def findings(cfg) -> list:
    """体检结论：[{path, level, detail}]。level ∈ bad / warn。"""
    out = []
    ever = _ever_snapshotted(cfg)
    today = dt.date.today()
    for item in entries():
        rel, why = item["path"], item["why"]
        path = cfg.state_dir / rel
        if not path.is_file():
            if rel in ever:
                out.append({"path": rel, "level": "bad",
                            "detail": f"{rel} 不见了（历史快照里有，说明被删过）——{why}",
                            "fix": f"从 {snapshot_root(cfg)}/<最近时间戳>/ 拷回"})
            continue
        days = item["stale_days"]
        if not days:
            continue
        mtime = dt.date.fromtimestamp(path.stat().st_mtime)
        age = (today - mtime).days
        if age > days:
            out.append({"path": rel, "level": "warn",
                        "detail": f"{rel} 已 {age} 天没更新（期望 {days} 天内）",
                        "fix": "确认对应功能还在跑（巡检 / 检索），或它已经没用了"})
    return out


def summarize(cfg) -> dict:
    present = [item["path"] for item in entries() if (cfg.state_dir / item["path"]).is_file()]
    return {"present": len(present), "total": len(entries()), "paths": present,
            "snapshots": len(snapshots(cfg))}


def render_findings(findings_: list) -> str:
    if not findings_:
        return "运行数据：没有发现丢失或过期（新装机器上清单里的文件本来就不存在，不算问题）"
    lines = [f"运行数据：{len(findings_)} 项要处理"]
    for item in findings_:
        icon = "❌" if item["level"] == "bad" else "⚠️"
        lines.append(f"  {icon} {item['detail']}")
        if item.get("fix"):
            lines.append(f"     修复：{item['fix']}")
    return "\n".join(lines)


__all__ = ["DURABLE", "README_NAME", "SNAPSHOT_ROOT", "DEFAULT_KEEP", "entries", "readme_path",
           "write_readme", "snapshot_root", "snapshots", "snapshot", "findings", "summarize",
           "render_findings"]
