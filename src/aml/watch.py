"""常驻监听：会话结束就自动入库（并交给蒸馏）。

两种触发，都是从实战里定下来的：

1. **DSH 归档**（`workspace.json` 的 `archivedSessionIds` 多了一条）
   —— 用户在界面里归档 = 明确"这段结束了"，**不等文件冷却**，立刻入库。
2. **任何 agent 的会话文件静默 ≥ stable 秒**（默认 120s，说明不再写入）
   —— 视为对话已关闭，增量入库。

已处理的 `(路径 → size, mtime)` 和已归档 id 记在 `state/watch_state.json`，
所以每轮只处理新增/变化的部分。**不要把它当后台 job 挂在 agent 会话里**：
agent 一重启就被连坐杀掉；要用独立守护/计划任务拉起来（原系统为此踩过 4 小时静默挂掉的坑）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time

from . import ingest
from .adapters import build


class WatchState:
    def __init__(self, cfg):
        self.path = cfg.state_dir / "watch_state.json"
        self.data = {"files": {}, "archived": []}
        if self.path.is_file():
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data["files"] = loaded.get("files") or {}
                self.data["archived"] = loaded.get("archived") or []
            except (OSError, ValueError):
                pass

    def save(self):
        self.data["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


def session_files(cfg) -> dict:
    """{规范化路径: (size, mtime)}，覆盖所有启用适配器能看到的会话文件。"""
    out = {}
    for adapter in build(cfg):
        for path in adapter.discover():
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_size > 0:
                out[os.path.abspath(path)] = (st.st_size, int(st.st_mtime))
    return out


def cooled(files: dict, known: dict, stable_seconds: int, now: float | None = None) -> list:
    """新增或已变化、且已静默够久的文件（= 会话大概率已关闭）。"""
    now = now if now is not None else time.time()
    out = []
    for path, (size, mtime) in files.items():
        old = known.get(path)
        if old and old.get("size") == size and old.get("mtime") == mtime:
            continue
        if now - mtime < stable_seconds:
            continue
        out.append(path)
    return out


def should_distill(path: str) -> dict:
    """这个文件该不该进蒸馏队列、按什么身份（各 agent 的策略不同）。"""
    low = path.replace("/", "\\").lower()
    if low.endswith("wire.jsonl"):
        return {"distill": False, "reason": "kimi 只入库不蒸馏"}
    if "\\.claude\\" in low and low.endswith(".jsonl"):
        if "\\subagents\\" in low:
            return {"distill": False, "reason": "子代理记录太碎"}
        return {"distill": True, "agent": "claude-code",
                "session_id": os.path.basename(path)[:-6]}
    if "\\.dsh\\sessions\\" in low and low.endswith(".zstd"):
        sid = os.path.basename(os.path.dirname(path))
        return {"distill": True, "agent": "dsh",
                "session_id": sid if sid.startswith("session-") else f"session-{sid}",
                "min_tasks": 3}
    return {"distill": False, "reason": "未识别的来源"}


def cycle(cfg, state: WatchState, stable: int, distill_sink=None, dry_run: bool = False) -> dict:
    """跑一轮。返回本轮动作摘要。"""
    summary = {"archived_new": [], "ingested": [], "queued": []}
    files = session_files(cfg)

    # ① DSH 归档：立刻入库（不等冷却）
    archived_now = []
    for adapter in build(cfg):
        if hasattr(adapter, "archived_sessions"):
            archived_now = sorted(adapter.archived_sessions())
            break
    fresh = [sid for sid in archived_now if sid not in state.data["archived"]]
    for sid in fresh:
        short = sid.replace("session-", "")
        paths = [p for p in files if os.path.basename(os.path.dirname(p)).endswith(short[:36])]
        if paths and not dry_run:
            ingest.sync(cfg, only_files=paths, do_backfill=bool(cfg.get("db_path")))
        summary["archived_new"].append(sid)
        summary["ingested"] += paths
        if distill_sink:
            distill_sink({"session_id": sid, "agent": "dsh", "reason": "归档"})
            summary["queued"].append(short[:8])
    if fresh and not dry_run:
        state.data["archived"] = archived_now

    # ② 静默够久的会话文件：增量入库
    changed = cooled(files, state.data["files"], stable)
    if changed and not dry_run:
        ingest.sync(cfg, only_files=changed, do_backfill=bool(cfg.get("db_path")))
    summary["ingested"] += changed
    for path in changed:
        rule = should_distill(path)
        if rule.get("distill") and distill_sink:
            distill_sink({"session_id": rule["session_id"], "agent": rule.get("agent"),
                          "reason": "会话已关闭", "min_tasks": rule.get("min_tasks", 0)})
            summary["queued"].append(rule["session_id"].replace("session-", "")[:8])

    # 记录签名：只记"已经入库过"或"已冷却"的，没冷却的留到下一轮
    if not dry_run:
        for path, (size, mtime) in files.items():
            if path in changed or (time.time() - mtime) >= stable:
                state.data["files"][path] = {"size": size, "mtime": mtime}
        state.save()
    return summary


def seed(cfg, state: WatchState, dry_run: bool = False) -> dict:
    """首次启用：把现状记为基线（**不导入**），避免第一轮把全机历史重灌一遍。"""
    files = session_files(cfg)
    archived = []
    for adapter in build(cfg):
        if hasattr(adapter, "archived_sessions"):
            archived = sorted(adapter.archived_sessions())
            break
    if not dry_run:
        for path, (size, mtime) in files.items():
            state.data["files"][path] = {"size": size, "mtime": mtime}
        state.data["archived"] = archived
        state.save()
    return {"files": len(files), "archived": len(archived)}


def run(cfg, once: bool = False, interval: int = 60, stable: int = 120,
        do_seed: bool = False, dry_run: bool = False, log=print,
        distill_sink=None) -> int:
    """主循环。distill_sink 由调用方注入（通常是 distill.enqueue），便于测试与解耦。"""
    state = WatchState(cfg)
    if do_seed:
        info = seed(cfg, state, dry_run=dry_run)
        log(f"已记录基线：{info['files']} 个会话文件，{info['archived']} 个已归档会话"
            + ("（dry-run 未落盘）" if dry_run else ""))
        return 0

    log(f"启动：间隔 {interval}s，静默阈值 {stable}s，"
        f"已记录 {len(state.data['files'])} 个文件 / {len(state.data['archived'])} 个已归档会话")
    while True:
        try:
            summary = cycle(cfg, state, stable, distill_sink=distill_sink, dry_run=dry_run)
            if summary["ingested"] or summary["archived_new"]:
                log(f"本轮：归档 {len(summary['archived_new'])} 个，入库 {len(summary['ingested'])} 个文件，"
                    f"入蒸馏队列 {len(summary['queued'])} 个")
        except Exception as e:  # noqa: BLE001 - 监听器不能因为一轮出错就退出
            log(f"⚠ 本轮出错：{type(e).__name__} {str(e)[:160]}")
        if once:
            break
        time.sleep(interval)
    return 0
