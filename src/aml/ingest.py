"""采集：会话文件 → 记忆层（保留原始时间戳）。

每个 turn 产出两条记忆：
  kind:task  —— 用户当时说的话
  kind:reply —— agent 的可见回复 + 用到的工具名

标签体系（检索全靠它）：
  agent:<谁> / session:<前8位> / project:<项目> / date:<YYYY-MM-DD> / kind:task|reply
"""
from __future__ import annotations

import sqlite3
import time

from . import text
from .adapters import build
from .http import MemoryClient, client_for


def collect(cfg, only_files=None, skip_files=None, since: str | None = None) -> list:
    """采集所有启用适配器的 turn，转成待写入的记忆记录。"""
    records = []
    max_len = int(cfg.section("ingest").get("max_len", 300))
    min_len = int(cfg.section("ingest").get("min_len", 8))
    for adapter in build(cfg, only_files=only_files, skip_files=skip_files):
        for path in adapter.discover():
            for turn in adapter.turns(path):
                if since and turn.ts and turn.ts < since:
                    continue
                day = (turn.ts or "")[:10]
                tags = [f"agent:{turn.agent}", f"session:{turn.session[:8]}",
                        f"project:{turn.project}", f"date:{day}"]
                meta = {"timestamp": turn.ts, "source_agent": turn.agent,
                        "session_id": turn.session, "project": turn.project,
                        "source_path": turn.path, "source": "session-import"}
                if turn.user and text.keep(turn.user, min_len):
                    records.append({"content": text.clip(turn.user, max_len),
                                    "tags": tags + ["kind:task"],
                                    "metadata": dict(meta, kind="task"),
                                    "conversation_id": turn.session})
                body = text.clip(turn.reply, max_len)
                if body and text.keep(body, min_len):
                    if turn.tools:
                        names = sorted(set(turn.tools))[:8]
                        body = f"{body}（用了工具：{', '.join(names)}）"[:max_len]
                    records.append({"content": body,
                                    "tags": tags + ["kind:reply"],
                                    "metadata": dict(meta, kind="reply",
                                                     tools=sorted(set(turn.tools))[:20]),
                                    "conversation_id": turn.session})
    return records


def write(cfg, records, client: MemoryClient | None = None, limit: int = 0, progress=None) -> dict:
    """写进记忆层。服务端按 content_hash 去重，所以可反复跑。"""
    client = client or client_for(cfg)
    if limit:
        records = records[:limit]
    ok = dup = err = 0
    started = time.time()
    for i, rec in enumerate(records, 1):
        try:
            res = client.store(rec["content"], rec["tags"], rec["metadata"], rec.get("conversation_id"))
            if res.get("success"):
                ok += 1
            else:
                dup += 1
        except Exception:  # noqa: BLE001
            err += 1
        if progress and (i % 50 == 0 or i == len(records)):
            progress(i, len(records), ok, err, i / max(time.time() - started, 1e-6))
    return {"total": len(records), "ok": ok, "dup": dup, "error": err,
            "seconds": round(time.time() - started, 1)}


def backfill(cfg, client: MemoryClient | None = None) -> dict:
    """把 created_at 从 metadata.timestamp 回填成**原始时间**。

    服务的写入 API 永远把 created_at 记成"写入时刻"，原始时间只留在 metadata 里；
    不做这一步，时间轴检索就是错的（历史全挤在今天）。
    """
    db = str(cfg.get("db_path") or "")
    if not db:
        raise RuntimeError("需要配置 db_path（记忆服务的 SQLite 库）才能回填时间")
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()
    rows = cur.execute("SELECT content_hash, metadata, created_at FROM memories").fetchall()
    fixed = 0
    for content_hash, meta, created in rows:
        try:
            import json
            ts = (json.loads(meta or "{}") or {}).get("timestamp")
        except ValueError:
            continue
        if not ts:
            continue
        try:
            import datetime as dt
            epoch = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if abs(epoch - (created or 0)) < 60:
            continue
        stamp = ts.replace("+00:00", "Z")
        cur.execute("UPDATE memories SET created_at=?, created_at_iso=?, updated_at=?, updated_at_iso=? "
                    "WHERE content_hash=?", (epoch, stamp, epoch, stamp, content_hash))
        fixed += 1
    con.commit()
    con.close()
    return {"rows": len(rows), "fixed": fixed}


def sync(cfg, since: str | None = None, dry_run: bool = False, limit: int = 0,
         only_files=None, skip_files=None, do_backfill: bool = True, progress=None) -> dict:
    """一次完整同步：采集 → 写入 → 回填时间。"""
    records = collect(cfg, only_files=only_files, skip_files=skip_files, since=since)
    summary = {"collected": len(records)}
    if dry_run:
        summary["by_agent"] = _by_agent(records)
        return summary
    summary["write"] = write(cfg, records, limit=limit, progress=progress)
    if do_backfill and cfg.get("db_path"):
        summary["backfill"] = backfill(cfg)
    return summary


def _by_agent(records) -> dict:
    out = {}
    for r in records:
        key = r["metadata"]["source_agent"]
        out[key] = out.get(key, 0) + 1
    return out
