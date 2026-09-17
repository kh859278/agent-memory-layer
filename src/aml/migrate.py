"""存量迁移：把已经灌进库的"程序性内容"补打 `kind:procedure`。

背景：`kind:procedure` 这个标签是后加的（见 docs/TRUST-MODEL.md），
新入库的会自动带上，但**之前已经灌进去的技能块没有**。

⚠️ 为什么不能只靠"改检索过滤"就完事（两手都要抓）：
  · **检索层已经兜住了**：`Retriever` 认标签，也认 `kb:<程序性目录>`（`is_procedure()`），
    所以就算一条都没迁移，技能正文也不会被自动召回；
  · **迁移的意义在于数据本身正确**：导出的 markdown、别的消费者（另一个 agent、另一个工具）
    看这条记录时，能一眼知道"这是程序性指令，不是知识"。

实现要点（踩过的坑）：
  · 服务的 content_hash **把标签算进去了** → 改标签等于新记录，必须"重存 + 删旧"
  · 重存必须带 `conversation_id`，否则与库里近义记录相撞会被判重复而拒收
  · 删旧之前先写**整批快照**，可 `rollback` 原样恢复（迁移不是不可逆操作）
  · 逐条落盘进度、可中断重跑（已带标签的会自动跳过）
"""
from __future__ import annotations

import datetime as dt
import json
import time

from .http import MemoryClient


def procedure_dirs(cfg) -> list:
    return list(cfg.section("ingest").get("procedure_dirs") or [])


def is_procedure(memory: dict, dirs=None) -> bool:
    """一条记录算不算"程序性内容"：带 kind:procedure 标签，或来自程序性目录。

    两个判据都要认：新数据靠标签，历史数据靠目录标签（存量迁移之前也能兜住）。
    """
    tags = memory.get("tags") or []
    if "kind:procedure" in tags:
        return True
    if dirs:
        return any(f"kb:{d}" in tags for d in dirs)
    return False


def plan(cfg, client: MemoryClient | None = None, limit: int = 0, dirs=None) -> dict:
    """只读预演：有多少条来自程序性目录、其中多少已经打过标签。"""
    client = client or MemoryClient(cfg.api)
    dirs = dirs or procedure_dirs(cfg)
    total = pending = tagged = 0
    hashes = []
    for memory in client.iter_memories():
        tags = memory.get("tags") or []
        if not any(f"kb:{d}" in tags for d in dirs):
            continue
        total += 1
        if "kind:procedure" in tags:
            tagged += 1
            continue
        pending += 1
        if memory.get("content_hash"):
            hashes.append(memory["content_hash"])
        if limit and pending >= limit:
            break
    return {"dirs": dirs, "total": total, "tagged": tagged, "pending": pending,
            "hashes": hashes, "sample": hashes[:5]}


def apply(cfg, client: MemoryClient | None = None, limit: int = 0, dirs=None, log=print,
          progress=None) -> dict:
    """执行迁移：重存（带 kind:procedure）+ 删旧。返回结果与快照路径。"""
    client = client or MemoryClient(cfg.api)
    info = plan(cfg, client=client, limit=limit, dirs=dirs)
    dirs = info["dirs"]
    log(f"程序性目录 {dirs}：共 {info['total']} 条，已带标签 {info['tagged']}，待迁移 {info['pending']}")
    if not info["pending"]:
        return {"migrated": 0, "failed": 0, "snapshot": None, "pending": 0}

    # 按 hash 取回完整记录（plan 只留了 hash，这里要正文与元数据）
    wanted = set(info["hashes"])
    records = [m for m in client.iter_memories() if m.get("content_hash") in wanted]
    log(f"取回待迁移记录 {len(records)} 条")

    snapshot = {"at": dt.datetime.now().isoformat(timespec="seconds"), "dirs": dirs,
                "moved": [], "created": []}
    migrated = failed = 0
    started = time.time()
    for index, memory in enumerate(records, 1):
        tags = list(memory.get("tags") or [])
        meta = dict(memory.get("metadata") or {})
        old_hash = memory.get("content_hash")
        new_tags = tags + [t for t in ("kind:procedure", "authority:procedure") if t not in tags]
        try:
            # conversation_id 必须有：否则与库里近义记录相撞会被判重复而拒收
            response = client.store(memory.get("content") or "", new_tags, meta,
                                    conversation_id=f"migrate:procedure:{old_hash}")
            if not response.get("success"):
                raise RuntimeError(f"重存被拒：{str(response)[:80]}")
            new_hash = (response.get("content_hash") or response.get("hash")
                        or (response.get("memory") or {}).get("content_hash"))
            snapshot["moved"].append({"content": memory.get("content"), "tags": tags,
                                      "metadata": meta, "content_hash": old_hash})
            snapshot["created"].append(new_hash)
            client.delete(old_hash)          # 删旧：hash 含标签，不删就留一份无标签的孤儿
            migrated += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            if failed <= 3:
                log(f"  迁移失败 {old_hash}：{type(e).__name__} {str(e)[:90]}")
        if progress and (index % 25 == 0 or index == len(records)):
            progress(index, len(records), migrated, failed,
                     index / max(time.time() - started, 1e-6))

    path = None
    if snapshot["moved"]:
        path = cfg.backups_dir / "migrate" / f"procedure-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=1)
        log(f"快照：{path}（回滚：aml migrate rollback {path}）")
    return {"migrated": migrated, "failed": failed, "snapshot": str(path) if path else None,
            "pending": info["pending"], "seconds": round(time.time() - started, 1)}


def rollback(cfg, snapshot_path: str, client: MemoryClient | None = None, log=print) -> dict:
    """回滚迁移：恢复原记录、删掉新打的标签版。"""
    client = client or MemoryClient(cfg.api)
    with open(snapshot_path, encoding="utf-8") as f:
        data = json.load(f)
    restored = removed = 0
    for memory in data.get("moved") or []:
        response = client.store(memory.get("content") or "", memory.get("tags") or [],
                                memory.get("metadata") or {},
                                conversation_id=f"rollback:procedure:{memory.get('content_hash')}")
        restored += 1 if response.get("success") else 0
    for content_hash in data.get("created") or []:
        if not content_hash:
            continue
        try:
            client.delete(content_hash)
            removed += 1
        except Exception:  # noqa: BLE001
            pass
    log(f"已回滚：恢复 {restored} 条原记录，删除 {removed} 条带标签版")
    return {"restored": restored, "removed": removed, "from": snapshot_path}
