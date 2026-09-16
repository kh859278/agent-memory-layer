"""维护功能测试：备份/恢复是"数据安全"的底线，必须有测试盯着。

用临时造的小 SQLite 库（结构与记忆服务一致的最小集），不碰真实数据。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import maintenance  # noqa: E402


def make_db(path, rows=3):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memories (content_hash TEXT, content TEXT, tags TEXT, metadata TEXT, "
                "created_at REAL, deleted_at REAL)")
    for i in range(rows):
        con.execute("INSERT INTO memories VALUES (?,?,?,?,?,NULL)",
                    (f"h{i}", f"记忆 {i}", json.dumps(["kind:knowledge"]), json.dumps({}), 1700000000 + i))
    con.commit()
    con.close()
    return path


def make_cfg(tmp_path, db):
    return cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})


def test_backup_then_list(tmp_path):
    db = make_db(tmp_path / "sqlite_vec.db")
    cfg = make_cfg(tmp_path, db)
    info = maintenance.backup(cfg, force=True, keep=3)
    assert info["created"] and info["size_mb"] is not None
    rows = maintenance.list_backups(cfg)
    assert len(rows) == 1
    assert os.path.getsize(rows[0]["path"]) > 0


def test_backup_is_idempotent_per_day(tmp_path):
    db = make_db(tmp_path / "sqlite_vec.db")
    cfg = make_cfg(tmp_path, db)
    maintenance.backup(cfg, force=True)
    second = maintenance.backup(cfg)                 # 同一天第二次
    assert second["skipped"] is True
    assert len(maintenance.list_backups(cfg)) == 1
    forced = maintenance.backup(cfg, force=True)
    assert forced["skipped"] is False
    assert len(maintenance.list_backups(cfg)) == 2


def test_backup_prunes_old(tmp_path):
    db = make_db(tmp_path / "sqlite_vec.db")
    cfg = make_cfg(tmp_path, db)
    for _ in range(4):
        maintenance.backup(cfg, force=True, keep=2)
    assert len(maintenance.list_backups(cfg)) == 2


def test_restore_is_preview_by_default_and_applies_with_confirm(tmp_path):
    db = make_db(tmp_path / "sqlite_vec.db", rows=3)
    cfg = make_cfg(tmp_path, db)
    maintenance.backup(cfg, force=True)
    backup_name = maintenance.list_backups(cfg)[0]["name"]

    # 备份之后又写了数据：恢复应当把它退回到备份时的样子
    con = sqlite3.connect(db)
    con.execute("INSERT INTO memories VALUES ('h9','后来的','[]','{}',1700009999,NULL)")
    con.commit()
    con.close()
    assert maintenance.verify_db(str(db))["memories"] == 4

    preview = maintenance.restore(cfg, name=backup_name, confirm=False)
    assert preview["applied"] is False and "hint" in preview
    assert maintenance.verify_db(str(db))["memories"] == 4      # 预检不动数据

    applied = maintenance.restore(cfg, name=backup_name, confirm=True)
    assert applied["applied"] is True
    assert applied["after"]["memories"] == 3                     # 已回退
    assert os.path.isfile(applied["safety_copy"])                # 恢复前的库被另存，能反悔


def test_restore_refuses_broken_backup(tmp_path):
    db = make_db(tmp_path / "sqlite_vec.db")
    cfg = make_cfg(tmp_path, db)
    maintenance.backup(cfg, force=True)
    broken = tmp_path / "backups" / "memory_backup_20200101_000000.db"
    broken.write_bytes(b"not a database")
    try:
        maintenance.restore(cfg, name=broken.name, confirm=True)
    except RuntimeError as e:
        assert "拒绝恢复" in str(e)
    else:  # pragma: no cover
        raise AssertionError("坏备份应当被拒绝")


def test_export_markdown_groups_by_domain_and_project(tmp_path, monkeypatch):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    memories = [
        {"content": "【坑】某条跨项目经验", "tags": ["kind:knowledge", "domain:env-windows"],
         "metadata": {"domain": "env-windows", "title": "坑", "ktype": "pitfall",
                      "review_after": "2027-01-01"}, "created_at_iso": "2026-01-02T00:00:00Z"},
        {"content": "某项目里的一句话", "tags": ["project:demo"], "metadata": {},
         "created_at_iso": "2026-01-03T00:00:00Z"},
    ]

    class Fake:
        def __init__(self, *a, **k):
            pass

        def _request(self, *a, **k):
            return {"memories": memories, "has_more": False}

    monkeypatch.setattr(maintenance, "MemoryClient", Fake)
    out = tmp_path / "export" / "dump.md"
    info = maintenance.export(cfg, str(out))
    text = out.read_text(encoding="utf-8")
    assert info["count"] == 2
    assert "沉淀：env-windows" in text
    assert "某条跨项目经验" in text
    assert "`demo`" in text


def test_denoise_preview_lists_noise_without_deleting(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, tmp_path / "sqlite_vec.db")
    make_db(cfg.get("db_path"), rows=1)          # 只有 h0

    class Fake:
        def __init__(self, *a, **k):
            pass

        def _request(self, *a, **k):
            return {"memories": [{"content_hash": "h0", "content": "好的", "tags": []}],
                    "has_more": False}

    monkeypatch.setattr(maintenance, "MemoryClient", Fake)
    preview = maintenance.denoise(cfg, apply=False)
    assert preview["candidates"] == 1 and preview["deleted"] == 0
    applied = maintenance.denoise(cfg, apply=True)
    assert applied["deleted"] == 1
    con = sqlite3.connect(str(cfg.get("db_path")))
    assert con.execute("SELECT deleted_at FROM memories WHERE content_hash='h0'").fetchone()[0] is not None
    con.close()
