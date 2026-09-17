"""召回账本：把"这次到底注入了哪几条记忆"记下来（append-only JSONL）。

为什么需要它（外部 review 说"反馈要自动采集"的落地前提）：
现在要给一条记忆打 `worked`，得先知道它的 hash。可 agent 手上通常只有**渲染后的文本**
（`[沉淀|env-windows|2026-09-13|0.85] …`），让它去翻 hash 就是逼它编。
有了账本，"刚才那次检索注入了什么"可查，`aml feedback --last 1 --outcome worked`
才能真正指得准。

三条设计取舍：
  · **append-only + 裁剪**（默认保留最近 500 条）：账本是运行数据，不该无限长
  · **写失败绝不抛**：检索不能因为记账失败而挂掉（记忆层是"锦上添花"，不是单点）
  · **正文不落盘**：只记 hash / 字数 / 阶段 / 查询 —— 账本可能被分享，
    别把内容带出去（与 `save_report` 不落注入正文同一个理由）
"""
from __future__ import annotations

import datetime as dt
import json
import os

DEFAULT_KEEP = 500


def log_path(cfg):
    return cfg.state_dir / "recall-log.jsonl"


def record(cfg, phase: str, query: str, hashes: list, chars: int = 0,
           project: str | None = None, source: str | None = None) -> dict | None:
    """记一次召回。返回事件（供调用方复用），任何失败都吞掉只返回 None。"""
    event = {
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "phase": phase,
        "query": (query or "")[:200],
        "project": project,
        "source": source,
        "chars": int(chars or 0),
        "hashes": [h for h in (hashes or []) if h],
    }
    try:
        path = log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return event


def read_all(cfg) -> list:
    """读全部事件（坏行跳过：账本损坏不该让反馈整体不可用）。"""
    path = log_path(cfg)
    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return events


def tail(cfg, n: int = 1) -> list:
    """最近 n 次召回（**新→旧**；`--last 1` = 最近那一次）。"""
    if n <= 0:
        return []
    return list(reversed(read_all(cfg)[-n:]))


def resolve(cfg, n: int = 1) -> dict:
    """把最近 n 次召回里的 hash 去重收集：{hash: **最近一次**包含它的事件}。

    顺序是"最近优先"（与 `tail` 一致）：同一条记忆被多次注入时，
    我们想看到的是**最近那次**它跟着什么问题被注入的。
    """
    targets = {}
    for event in tail(cfg, n):
        for h in event.get("hashes") or []:
            targets.setdefault(h, event)
    return targets


def prune(cfg, keep: int = DEFAULT_KEEP) -> int:
    """裁剪到最近 keep 条，返回删掉多少条。"""
    events = read_all(cfg)
    if len(events) <= keep:
        return 0
    kept = events[-keep:]
    path = log_path(cfg)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for event in kept:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return len(events) - len(kept)


def render(events: list) -> str:
    if not events:
        return "召回账本还是空的（检索一次就会有记录）"
    lines = [f"最近 {len(events)} 次召回（新→旧）："]
    for event in events:
        hashes = event.get("hashes") or []
        lines.append(f"  {event.get('at')}  {event.get('phase')}  "
                     f"{len(hashes)} 条 / {event.get('chars', 0)} 字  "
                     f"「{(event.get('query') or '')[:40]}」")
        for h in hashes[:6]:
            lines.append(f"      {h}")
        if len(hashes) > 6:
            lines.append(f"      …（还有 {len(hashes) - 6} 条）")
    lines.append("  给最近这几次打反馈：`aml feedback --last 1 --outcome worked`")
    return "\n".join(lines)


__all__ = ["DEFAULT_KEEP", "log_path", "record", "read_all", "tail", "resolve", "prune",
           "render"]
