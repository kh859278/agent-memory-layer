"""索引离线重建：服务分页卡住时的兜底路径（2026-11-20 实测卡过一小时）。"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import kb  # noqa: E402


def _db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memories (content TEXT, tags TEXT, metadata TEXT, "
                "created_at_iso TEXT, deleted_at REAL)")
    con.execute("INSERT INTO memories VALUES (?,?,?,?,NULL)",
                ("【A】x", "kind:knowledge,domain:tooling", json.dumps({"title": "A"}),
                 "2026-09-01T00:00:00Z"))
    con.execute("INSERT INTO memories VALUES (?,?,?,?,NULL)",
                ("【B】y", json.dumps(["kind:knowledge", "domain:agent-eval"]),
                 json.dumps({"title": "B"}), "2026-09-02T00:00:00Z"))
    con.execute("INSERT INTO memories VALUES (?,?,?,?,123.0)",
                ("已删", "kind:knowledge,domain:tooling", "{}", "2026-09-03T00:00:00Z"))
    con.commit()
    con.close()
    return path


def test_memories_from_db_normalizes_fields(tmp_path):
    db = _db(tmp_path / "sqlite_vec.db")
    cfg = cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})
    mems = kb.memories_from_db(cfg)
    assert len(mems) == 2, "软删行不该进来"
    assert mems[0]["tags"] == ["kind:knowledge", "domain:tooling"], "逗号串要拆成 list"
    assert mems[1]["tags"] == ["kind:knowledge", "domain:agent-eval"], "JSON 串也要认"
    assert mems[0]["metadata"]["title"] == "A"


def test_build_index_offline_writes_index(tmp_path):
    db = _db(tmp_path / "sqlite_vec.db")
    cfg = cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})
    info = kb.build_index(cfg, memories=kb.memories_from_db(cfg))
    assert info["memories"] == 2 and info["knowledge"] == 2
    text = (cfg.knowledge_dir / "索引.md").read_text(encoding="utf-8")
    assert "由 `aml index rebuild`" in text
