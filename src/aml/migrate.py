"""存量迁移：把已经灌进库的"程序性内容"补打 `kind:procedure`。

背景：`kind:procedure` 这个标签是后加的（见 docs/TRUST-MODEL.md），
新入库的会自动带上，但**之前已经灌进去的技能块没有**。

⚠️ 为什么不能只靠"改检索过滤"就完事（两手都要抓）：
  · **检索层已经兜住了**：`Retriever` 认标签，也认 `kb:<程序性目录>`（`is_procedure()`），
    所以就算一条都没迁移，技能正文也不会被自动召回；
  · **迁移的意义在于数据本身正确**：导出的 markdown、别的消费者看到这条记录时，
    能一眼知道"这是程序性指令，不是知识"。

实现要点（两个坑都实测踩过）：
  · **不能用"重存 + 删旧"**：服务的 content_hash 把标签算进去了，重存本意是生成新 hash，
    但服务会先按**逐字内容**判重 → `Duplicate content detected (exact match)`；
    带 `conversation_id` 只绕过**语义**去重，绕不过逐字去重（本机 2804 条全失败）。
  · **正确做法是原地改**：`PUT /api/memories/{hash}`，body 只有 `tags / memory_type / metadata`。
    不重新嵌入、秒级完成、hash 不变，别的引用不会失效。
  · 迁移前把每条的原 tags 写进快照，`rollback` 就是原样写回。
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
    records = []
    for memory in client.iter_memories():
        tags = memory.get("tags") or []
        if not any(f"kb:{d}" in tags for d in dirs):
            continue
        total += 1
        if "kind:procedure" in tags:
            tagged += 1
            continue
        pending += 1
        records.append({"content_hash": memory.get("content_hash"), "tags": tags})
        if limit and pending >= limit:
            break
    return {"dirs": dirs, "total": total, "tagged": tagged, "pending": pending,
            "records": records, "sample": [r["content_hash"][:12] for r in records[:5]]}


def apply(cfg, client: MemoryClient | None = None, limit: int = 0, dirs=None, log=print,
          progress=None) -> dict:
    """执行迁移：原地加标签（PUT），完成后写快照（可回滚）。"""
    client = client or MemoryClient(cfg.api)
    info = plan(cfg, client=client, limit=limit, dirs=dirs)
    log(f"程序性目录 {info['dirs']}：共 {info['total']} 条，已带标签 {info['tagged']}，"
        f"待迁移 {info['pending']}")
    if not info["pending"]:
        return {"migrated": 0, "failed": 0, "snapshot": None, "pending": 0}

    snapshot = {"at": dt.datetime.now().isoformat(timespec="seconds"), "dirs": info["dirs"],
                "moved": []}
    migrated = failed = 0
    started = time.time()
    for index, record in enumerate(info["records"], 1):
        content_hash, old_tags = record["content_hash"], list(record["tags"])
        new_tags = old_tags + [t for t in ("kind:procedure", "authority:procedure")
                               if t not in old_tags]
        try:
            response = client.update(content_hash, {"tags": new_tags})
            if not response.get("success", True):
                raise RuntimeError(f"更新被拒：{str(response)[:80]}")
            snapshot["moved"].append({"content_hash": content_hash, "tags": old_tags})
            migrated += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            if failed <= 3:
                log(f"  迁移失败 {str(content_hash)[:12]}：{type(e).__name__} {str(e)[:90]}")
        if progress and (index % 100 == 0 or index == len(info["records"])):
            progress(index, len(info["records"]), migrated, failed,
                     index / max(time.time() - started, 1e-6))

    path = None
    if snapshot["moved"]:
        path = cfg.backups_dir / "migrate" / f"procedure-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=1)
        log(f"快照：{path}（回滚：aml migrate rollback --file {path}）")
    return {"migrated": migrated, "failed": failed, "snapshot": str(path) if path else None,
            "pending": info["pending"], "seconds": round(time.time() - started, 1)}


def rollback(cfg, snapshot_path: str, client: MemoryClient | None = None, log=print) -> dict:
    """回滚迁移：把原 tags 一条条写回去。"""
    client = client or MemoryClient(cfg.api)
    with open(snapshot_path, encoding="utf-8") as f:
        data = json.load(f)
    restored = failed = 0
    for record in data.get("moved") or []:
        try:
            response = client.update(record["content_hash"], {"tags": record["tags"]})
            restored += 1 if response.get("success", True) else 0
        except Exception:  # noqa: BLE001
            failed += 1
    log(f"已回滚：恢复 {restored} 条 tags，失败 {failed} 条")
    return {"restored": restored, "failed": failed, "from": snapshot_path}
