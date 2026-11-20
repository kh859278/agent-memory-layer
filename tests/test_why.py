"""`aml why` 与 `--trace`：让"这条记忆凭什么被引用"可查。

这两个是质量闭环的入口：没有 hash 可核对，就没有人能打 worked/failed，
decay / 整理 / 排序全部拿不到数据。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import maintenance  # noqa: E402
from aml.retrieval import Result  # noqa: E402

HASH = "abcdef1234567890" + "0" * 48


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memories (content_hash TEXT, content TEXT, tags TEXT, metadata TEXT, "
                "created_at_iso TEXT, deleted_at REAL, superseded_by TEXT, confidence REAL)")
    for h, content, tags, meta in rows:
        raw_tags = tags if isinstance(tags, str) else json.dumps(tags)
        con.execute("INSERT INTO memories VALUES (?,?,?,?,?,NULL,?,0.8)",
                    (h, content, raw_tags, json.dumps(meta), "2026-09-18T10:00:00Z", ""))
    con.commit()
    con.close()
    return path


def make_cfg(tmp_path, db):
    return cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})


def test_why_returns_provenance_and_recall_count(tmp_path):
    db = make_db(tmp_path / "sqlite_vec.db", [
        (HASH, "结论：以后只用 jj 提交", ["kind:knowledge", "domain:agent-workflow"],
         {"source": "人工", "review_after": "2027-01-01"}),
    ])
    cfg = make_cfg(tmp_path, db)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / "recall-log.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"at": "2026-11-20T19:00:00", "phase": "P2",
                            "hashes": [HASH]}, ensure_ascii=False) + "\n")

    info = maintenance.why(cfg, HASH[:8])
    assert info["ok"] is True
    assert info["hash"] == HASH
    assert info["metadata"]["source"] == "人工"
    assert info["review_after"] == "2027-01-01"
    assert info["recall"]["count"] == 1


def test_why_parses_comma_separated_tags(tmp_path):
    """后端真实格式是逗号串；解析失败会让领域/项目静默变成空。"""
    db = make_db(tmp_path / "sqlite_vec.db", [
        (HASH, "正文", "kind:knowledge,domain:env-windows,project:demo", {}),
    ])
    cfg = make_cfg(tmp_path, db)
    info = maintenance.why(cfg, HASH[:6])
    assert "domain:env-windows" in info["tags"]
    assert "project:demo" in info["tags"]


def test_why_reports_missing(tmp_path):
    db = make_db(tmp_path / "sqlite_vec.db", [])
    cfg = make_cfg(tmp_path, db)
    info = maintenance.why(cfg, "deadbeef")
    assert info["ok"] is False and "没有" in info["error"]


def test_render_trace_appends_hash():
    res = Result("P2", ["[沉淀|d|2026-09-18|0.85] 正文"], 10, 600, ["沉淀"], {}, [HASH])
    plain = res.render()
    traced = res.render(trace=True)
    assert "[h:" not in plain
    assert "[h:abcdef12]" in traced


def test_render_trace_without_hashes_is_safe():
    res = Result("P2", ["x"], 1, 600, [], {}, [])
    assert "[h:" not in res.render(trace=True)
