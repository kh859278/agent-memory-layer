"""补齐没有向量的记忆记录。

问题：库里有记录缺 embedding（多为高价值的 `kind:knowledge` 沉淀），
**语义检索永远搜不到它们**，只能靠关键词碰运气。症状很隐蔽：不报错，只是"查不到"。

原因：写入时嵌入失败/超时——记录落库了，向量没落。

做法：把缺向量的记录重新 POST 到 `/api/memories`（服务端会重新嵌入），
然后删掉旧的孤儿记录。**关键坑**：重存时必须带 `conversation_id`，
否则与库里已有记录语义相近时会被判 "Duplicate content detected" 而拒绝入库，
那条记录就永远拿不到向量（原系统 2026-09-16 实测踩过）。

默认只预览：`--dry-run` 数一遍，`--apply` 才真写。
"""
from __future__ import annotations

import json
import sqlite3
import time

from .http import MemoryClient, client_for

MISSING_SQL = """SELECT m.rowid, m.content, m.tags, m.metadata
                 FROM memories m
                 LEFT JOIN memory_embeddings_rowids r ON r.rowid = m.rowid
                 WHERE r.rowid IS NULL
                 ORDER BY m.rowid"""

ORPHAN_SQL = """SELECT m.rowid, m.content_hash, m.content
                FROM memories m
                LEFT JOIN memory_embeddings_rowids r ON r.rowid = m.rowid
                WHERE r.rowid IS NULL
                  AND EXISTS (SELECT 1 FROM memories m2
                              JOIN memory_embeddings_rowids r2 ON r2.rowid = m2.rowid
                              WHERE m2.content = m.content AND m2.rowid <> m.rowid)"""


def _connect(cfg, read_only: bool = True):
    db = str(cfg.get("db_path") or "")
    if not db:
        raise RuntimeError("需要配置 db_path（记忆服务的 SQLite 库）才能直连检查向量表")
    # 检查用只读（服务在跑也安全）；只有 prune 删孤儿才需要可写连接
    uri = f"file:{db}?mode=ro" if read_only else f"file:{db}"
    return sqlite3.connect(uri, uri=True, timeout=30)


def _rows(raw):
    """tags/metadata 可能是 JSON 串也可能是列表——两种都见。"""
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        if isinstance(raw, str):
            return [t.strip() for t in raw.split(",") if t.strip()]
        return []


def stats(cfg) -> dict:
    """只读统计：总数、缺向量数、可清理的孤儿数。"""
    try:
        con = _connect(cfg)
    except RuntimeError as e:
        return {"error": str(e)}
    try:
        cur = con.cursor()
        total = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        missing = cur.execute(MISSING_SQL).fetchall()
        try:
            orphans = cur.execute(ORPHAN_SQL).fetchall()
        except sqlite3.Error:
            orphans = []
        return {"total": total, "missing": len(missing), "orphans": len(orphans),
                "sample": [(row[0], (row[2] or "")[:60]) for row in missing[:5]]}
    except sqlite3.Error as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        con.close()


def prune_orphans(cfg, apply: bool = False) -> dict:
    """删掉"没有向量、但库里已有同内容且有向量的副本"的孤儿记录（它们重存没意义）。"""
    try:
        con = _connect(cfg, read_only=False)
    except RuntimeError as e:
        return {"error": str(e)}
    try:
        cur = con.cursor()
        orphans = cur.execute(ORPHAN_SQL).fetchall()
        if not apply or not orphans:
            return {"orphans": len(orphans), "deleted": 0, "applied": apply,
                    "sample": [(row[0], (row[2] or "")[:60]) for row in orphans[:5]]}
        for rowid, _hash, _content in orphans:
            cur.execute("DELETE FROM memories WHERE rowid=?", (rowid,))
        con.commit()
        return {"orphans": len(orphans), "deleted": len(orphans), "applied": True}
    finally:
        con.close()


def backfill(cfg, apply: bool = False, limit: int = 0, client: MemoryClient | None = None,
             progress=None, log=print) -> dict:
    """把缺向量的记录重存一遍（服务端重新嵌入）。"""
    info = stats(cfg)
    if info.get("error"):
        return info
    log(f"库内 {info['total']} 条，缺向量 {info['missing']} 条"
        + (f"，其中可清理的孤儿 {info['orphans']} 条" if info["orphans"] else ""))
    if not info["missing"]:
        return {"total": info["total"], "missing": 0, "ok": 0, "failed": 0, "applied": apply}
    if not apply:
        for rowid, preview in info["sample"]:
            log(f"  rowid={rowid}  {preview!r}")
        log("（预览模式；确认后加 --apply，或先 --prune-orphans 清掉重复的孤儿）")
        return {"total": info["total"], "missing": info["missing"], "ok": 0, "failed": 0,
                "applied": False, "dry_run": True}

    con = _connect(cfg)
    try:
        rows = con.execute(MISSING_SQL).fetchall()
    finally:
        con.close()
    if limit:
        rows = rows[:limit]

    client = client or client_for(cfg)
    ok = failed = 0
    started = time.time()
    for index, (rowid, content, tags_raw, meta_raw) in enumerate(rows, 1):
        meta = _rows(meta_raw)
        if not isinstance(meta, dict):
            meta = {}
        payload_meta = {k: meta[k] for k in
                        ("source_agent", "src_session", "timestamp", "kind") if k in meta}
        try:
            # conversation_id 是必须的：不带它，语义相近会被判重复而拒收，永远补不上向量
            response = client.store(content, _rows(tags_raw) or [], payload_meta,
                                    conversation_id="embed-backfill")
            if response.get("success"):
                ok += 1
            else:
                failed += 1
                if failed <= 3:
                    log(f"  rowid={rowid} 被拒：{str(response.get('message'))[:80]}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            if failed <= 3:
                log(f"  rowid={rowid} 失败：{type(e).__name__} {str(e)[:80]}")
        if progress and (index % 5 == 0 or index == len(rows)):
            progress(index, len(rows), ok, failed, index / max(time.time() - started, 1e-6))

    after = stats(cfg)
    log(f"完成：成功 {ok}，失败 {failed}")
    log(f"复查：库内 {after.get('total')} 条，仍缺向量 {after.get('missing')} 条")
    return {"total": info["total"], "missing": info["missing"], "ok": ok, "failed": failed,
            "remaining": after.get("missing"), "applied": True}
