"""维护：备份 / 恢复 / 导出 / 复核 / 降噪。

这一块是"数据安全"，比功能更重要：
  · 全部记忆只在一个 SQLite 文件里（本机实测 ~50 MB / 9700 条），**坏了就是全没了**；
  · 备份必须能在服务运行时做（在线备份 API，不会拷到写了一半的页）；
  · **光有备份没有恢复等于没备份** —— 这是抽取时发现的原系统缺口，本模块补上 restore；
  · 导出成人可读的 markdown：数据库之外还得有一份能直接读的副本，避免数据被锁死。
"""
from __future__ import annotations

import collections
import datetime as dt
import glob
import json
import os
import re
import shutil
import sqlite3

from .http import MemoryClient

SMALL_STATE = ("watch_state.json", "distill_queue.json", "lookup_state.json")


# --------------------------------------------------------------------- 备份

def backup(cfg, force: bool = False, keep: int = 7) -> dict:
    db = str(cfg.get("db_path") or "")
    dest = cfg.backups_dir
    if not db or not os.path.isfile(db):
        raise FileNotFoundError(f"找不到数据库：{db or '(未配置 db_path)'}")
    dest.mkdir(parents=True, exist_ok=True)

    today = dt.date.today().strftime("%Y%m%d")
    existing = [p for p in glob.glob(str(dest / "memory_backup_*.db"))
                if dt.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y%m%d") == today]
    created = None
    if existing and not force:
        skipped = True
    else:
        skipped = False
        # 文件名带微秒：同一秒内连续备份两次不会互相覆盖（实测踩到过）
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        created = dest / f"memory_backup_{stamp}.db"
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(str(created))
        try:
            with dst:
                src.backup(dst)          # 在线备份：服务在跑也安全
        finally:
            dst.close()
            src.close()

    copied = []
    for name in SMALL_STATE:
        p = cfg.state_dir / name
        if p.is_file():
            try:
                shutil.copy2(p, dest / name)
                copied.append(name)
            except OSError:
                pass

    files = sorted(glob.glob(str(dest / "memory_backup_*.db")), key=os.path.getmtime, reverse=True)
    pruned = []
    for old in files[keep:]:
        try:
            os.remove(old)
            pruned.append(os.path.basename(old))
        except OSError:
            pass
    return {"created": str(created) if created else None, "skipped": skipped,
            "size_mb": round(os.path.getsize(created) / 1024 / 1024, 1) if created else None,
            "state_files": copied, "pruned": pruned, "kept": min(len(files), keep),
            "dir": str(dest)}


def list_backups(cfg) -> list:
    out = []
    for p in sorted(glob.glob(str(cfg.backups_dir / "memory_backup_*.db")),
                    key=os.path.getmtime, reverse=True):
        st = os.stat(p)
        out.append({"name": os.path.basename(p), "path": p,
                    "size_mb": round(st.st_size / 1024 / 1024, 1),
                    "mtime": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return out


# --------------------------------------------------------------------- 恢复

def verify_db(path: str) -> dict:
    """完整性检查 + 基本统计（恢复前/后都要看一眼，别把一个坏库盖上去）。"""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as e:
        return {"integrity": f"打不开：{e}", "memories": 0, "ok": False}
    try:
        cur = con.cursor()
        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
        total = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except sqlite3.Error as e:
        return {"integrity": f"{type(e).__name__}: {e}", "memories": 0, "ok": False}
    finally:
        con.close()
    return {"integrity": integrity, "memories": total, "ok": integrity == "ok" and total > 0}


def restore(cfg, name: str | None = None, target: str | None = None, confirm: bool = False) -> dict:
    """把某个备份恢复成数据库文件。

    默认恢复到配置里的 db_path（**先自动把现有库另存一份**，避免"恢复错了没法回头"）。
    confirm=False 时只做预检，不写任何东西。
    """
    backups = list_backups(cfg)
    if not backups:
        raise FileNotFoundError(f"没有可用的备份：{cfg.backups_dir}")
    chosen = None
    if name:
        chosen = next((b for b in backups if b["name"] == name), None)
        if not chosen:
            raise FileNotFoundError(f"没有这个备份：{name}")
    else:
        chosen = backups[0]
    dest = target or str(cfg.get("db_path") or "")
    if not dest:
        raise ValueError("未配置 db_path，需用 --to 指定恢复目标")

    info = verify_db(chosen["path"])
    plan = {"backup": chosen, "target": dest, "verify": info, "applied": False}
    if not confirm:
        plan["hint"] = "预检完成。确认无误后加 --yes 执行恢复（会先自动备份现有库）"
        return plan

    if not info["ok"]:
        raise RuntimeError(f"备份文件自身不完整（integrity={info['integrity']}，{info['memories']} 条），拒绝恢复")
    if os.path.isfile(dest):
        safety = f"{dest}.before-restore-{dt.datetime.now():%Y%m%d_%H%M%S}"
        shutil.copy2(dest, safety)
        plan["safety_copy"] = safety
    shutil.copy2(chosen["path"], dest)
    plan["applied"] = True
    plan["after"] = verify_db(dest)
    return plan


# --------------------------------------------------------------------- 导出

def export(cfg, out_path: str, fmt: str = "md", limit: int = 0) -> dict:
    """把记忆导成人能读的文件（markdown 按领域/项目分组，或原始 JSON）。"""
    client = MemoryClient(cfg.api)
    memories = []
    page = 1
    while True:
        try:
            data = client._request(f"/api/memories?page={page}&page_size=100", None, method="GET")
        except Exception:  # noqa: BLE001
            break
        items = data.get("memories") or []
        if not items:
            break
        memories += items
        if not data.get("has_more"):
            break
        page += 1
    if limit:
        memories = memories[:limit]

    out = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if fmt == "json":
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(memories, f, ensure_ascii=False, indent=1)
        return {"path": out, "count": len(memories), "fmt": "json"}

    knowledge = collections.defaultdict(list)
    projects = collections.defaultdict(list)
    sessions = collections.Counter()
    for m in memories:
        tags = m.get("tags") or []
        meta = m.get("metadata") or {}
        if "kind:knowledge" in tags:
            domain = meta.get("domain") or next((t.split(":", 1)[1] for t in tags
                                                 if t.startswith("domain:")), "general")
            knowledge[domain].append(m)
        else:
            project = next((t.split(":", 1)[1] for t in tags if t.startswith("project:")), "unknown")
            projects[project].append(m)
        for t in tags:
            if t.startswith("session:"):
                sessions[t] += 1

    lines = ["# 记忆层导出", "",
             f"> 导出于 {dt.datetime.now():%Y-%m-%d %H:%M}，共 {len(memories)} 条"
             f"（沉淀 {sum(len(v) for v in knowledge.values())} 条 / {len(knowledge)} 领域）。",
             "> 这是**纯文本副本**：没有本程序也能读、能 grep、能搬走。", ""]
    for domain in sorted(knowledge, key=lambda d: -len(knowledge[d])):
        lines += [f"## 沉淀：{domain}（{len(knowledge[domain])} 条）", ""]
        for m in knowledge[domain]:
            meta = m.get("metadata") or {}
            stamp = (m.get("created_at_iso") or "?")[:10]
            lines += [f"### {meta.get('title') or (m.get('content') or '')[:24]}", "",
                      f"- 复核期：{meta.get('review_after', '-')} ｜ 来源：{meta.get('src_session', '-')} ｜ {stamp}",
                      "", (m.get("content") or "").strip(), ""]
    lines += ["## 项目历史（未蒸馏的会话流水）", "",
              "| 项目 | 条数 |", "|---|---|"]
    for project in sorted(projects, key=lambda p: -len(projects[p])):
        lines.append(f"| `{project}` | {len(projects[project])} |")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return {"path": out, "count": len(memories), "domains": len(knowledge), "fmt": "md"}


# ------------------------------------------------------------- 复核 / 降噪

def review_due(cfg, within_days: int = 0) -> list:
    """列出已过复核期（或 N 天内到期）的知识条目。"""
    client = MemoryClient(cfg.api)
    try:
        items = client.search_by_tag(["kind:knowledge"], match_all=True, n=500)
    except Exception:  # noqa: BLE001
        return []
    today = dt.date.today()
    limit = today + dt.timedelta(days=within_days)
    out = []
    for m in items:
        meta = m.get("metadata") or {}
        raw = str(meta.get("review_after") or "").strip()
        if not raw:
            continue
        try:
            due = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        if due <= limit:
            out.append({"hash": m.get("content_hash"), "title": meta.get("title") or (m.get("content") or "")[:30],
                        "domain": meta.get("domain", "-"), "review_after": raw,
                        "overdue": due <= today, "days": (due - today).days})
    out.sort(key=lambda x: x["review_after"])
    return out


def postpone(cfg, content_hash: str, days: int = 180) -> dict:
    """复核无误：把复核期往后顺延。"""
    client = MemoryClient(cfg.api)
    new_date = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    try:
        res = client._request("/api/memories/update", {"content_hash": content_hash,
                                                       "updates": {"metadata": {"review_after": new_date}}})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__} {e}"}
    return {"ok": True, "hash": content_hash, "review_after": new_date, "response": res}


NOISE_PATTERNS = [
    re.compile(r"标准模式.*(对话|轨迹).*系统提示词"),
    re.compile(r"上下文注入|skill-catalog|Insufficient Balance"),
    re.compile(r"^API Error: \d+"),
    re.compile(r"^[\s\W_]+$"),
    re.compile(r"^(OK|ok|好的|收到|嗯|对|是的|可以|继续|开始|完成|谢谢)[。.!！]?$"),
]


def noise_candidates(cfg, limit: int = 500) -> list:
    """找出应删的噪声条目（界面回显、纯确认语、纯符号）。"""
    client = MemoryClient(cfg.api)
    page, out = 1, []
    while len(out) < limit:
        try:
            data = client._request(f"/api/memories?page={page}&page_size=100", None, method="GET")
        except Exception:  # noqa: BLE001
            break
        items = data.get("memories") or []
        if not items:
            break
        for m in items:
            content = (m.get("content") or "").strip()
            if len(content) < 8 or any(p.search(content) for p in NOISE_PATTERNS):
                out.append({"hash": m.get("content_hash"), "content": content[:60],
                            "tags": m.get("tags") or []})
        if not data.get("has_more"):
            break
        page += 1
    return out[:limit]


def denoise(cfg, apply: bool = False) -> dict:
    """降噪：**软删除**（写 deleted_at，不物理删）—— 删错了还能捞回来。

    需要直连数据库；没配 db_path 时只返回候选清单。
    """
    candidates = noise_candidates(cfg)
    result = {"candidates": len(candidates), "deleted": 0, "applied": apply,
              "sample": candidates[:10]}
    if not apply:
        result["hint"] = "预演。确认后加 --apply 执行软删除（可回滚：UPDATE memories SET deleted_at=NULL）"
        return result
    db = str(cfg.get("db_path") or "")
    if not db or not os.path.isfile(db):
        result["error"] = "未配置 db_path，无法执行删除"
        return result
    hashes = [c["hash"] for c in candidates if c.get("hash")]
    con = sqlite3.connect(db, timeout=30)
    try:
        stamp = dt.datetime.now(dt.timezone.utc).timestamp()
        cur = con.cursor()
        for h in hashes:
            cur.execute("UPDATE memories SET deleted_at=? WHERE content_hash=? AND deleted_at IS NULL",
                        (stamp, h))
            result["deleted"] += cur.rowcount
        con.commit()
    finally:
        con.close()
    return result


def prune_export(cfg, keep_days: int = 30) -> dict:
    """清理 state 目录里过期的导出/临时文件（导出是给人看的，别无限堆积）。"""
    cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
    removed = []
    for pattern in ("export-*.md", "export-*.json"):
        for path in glob.glob(str(cfg.state_dir / pattern)):
            try:
                if dt.datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                    os.remove(path)
                    removed.append(os.path.basename(path))
            except OSError:
                pass
    return {"removed": removed}

