"""样板文本过滤：2026-11-20 实测库里 25.7% 是 harness 自己注入的样板。

这批测试钉住三件事：
  ① 样板必须被 `keep()` 挡掉（否则采集又把它写回去）；
  ② 正文里**讨论**这些字样不能被误伤（只认开头）；
  ③ 存量清理走 `denoise` 的软删除，可回滚。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import maintenance, text  # noqa: E402

BOILERPLATE = [
    "<system-reminder> The available skill catalog …",
    "Time sampled while preparing turn 2, step 1: 2026-11-20T19:07:43+08:00[Asia/Shanghai]",
    "Current runtime context. This snapshot supersedes earlier runtime-context snapshots.",
    "[SCHEDULE REMINDER BATCH] This is a scheduled message from the user",
    "Background subagent 1f2c42f3 finished and will do no further work unless",
    "<goal_round> Objective: 监控采集任务",
]


def test_boilerplate_prefixes_are_recognized():
    for s in BOILERPLATE:
        assert text.is_boilerplate(s), s
        assert text.keep(s) is False, s


def test_real_content_mentioning_boilerplate_is_kept():
    real = "结论：采集规则要挡掉 Time sampled while preparing 这类样板，否则预算被吃满。"
    assert text.is_boilerplate(real) is False
    assert text.keep(real) is True


def test_keep_can_opt_in_to_boilerplate():
    """回填/迁移需要原样保留时，显式放行。"""
    s = BOILERPLATE[1]
    assert text.keep(s, allow_boilerplate=True) is True


def test_worthwhile_signal():
    assert text.is_worthwhile("结论：以后 jj 仓库只读，提交一律走守门器") is True
    assert text.is_worthwhile("好的，我这就开始处理") is False
    assert text.is_worthwhile("短") is False


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memories (content_hash TEXT, content TEXT, tags TEXT, metadata TEXT, "
                "created_at REAL, deleted_at REAL)")
    for h, content in rows:
        con.execute("INSERT INTO memories VALUES (?,?,?,?,?,NULL)",
                    (h, content, json.dumps(["kind:task"]), json.dumps({}), 1700000000))
    con.commit()
    con.close()
    return path


def test_denoise_soft_deletes_boilerplate_only(tmp_path):
    db = make_db(tmp_path / "sqlite_vec.db", [
        ("h1", "Time sampled while preparing turn 1, step 1: 2026-11-20"),
        ("h2", "<system-reminder> skill catalog"),
        ("h3", "标准模式 对话 系统提示词"),
        ("h4", "结论：以后采集只记决策与坑"),
    ])
    cfg = cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})

    preview = maintenance.denoise(cfg)
    assert preview["candidates"] == 3
    assert preview["by_kind"] == {"boilerplate": 2, "ui_echo": 1}
    assert preview["deleted"] == 0

    applied = maintenance.denoise(cfg, apply=True)
    assert applied["deleted"] == 3

    con = sqlite3.connect(db)
    alive = [r[0] for r in con.execute("SELECT content_hash FROM memories WHERE deleted_at IS NULL")]
    con.close()
    assert alive == ["h4"], "真实内容不能被删"


def test_denoise_is_reversible(tmp_path):
    db = make_db(tmp_path / "sqlite_vec.db", [("h1", "Time sampled while preparing turn 1")])
    cfg = cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})
    maintenance.denoise(cfg, apply=True)
    con = sqlite3.connect(db)
    con.execute("UPDATE memories SET deleted_at=NULL")
    con.commit()
    con.close()
    assert maintenance.denoise(cfg)["candidates"] == 1


def test_keyword_fallback_excludes_soft_deleted(tmp_path):
    """软删必须对**关键词兜底**同样生效——语义侧会过滤，FTS/LIKE 以前不会。

    2026-11-20 实测：降噪 8,295 条后，一条样板查询仍从 FTS 路径原样捞回 5 条。
    """
    db = tmp_path / "sqlite_vec.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE memories (rowid INTEGER PRIMARY KEY, content TEXT, tags TEXT, "
        "created_at_iso TEXT, content_hash TEXT, metadata TEXT, deleted_at REAL);"
        "CREATE VIRTUAL TABLE memory_content_fts USING fts5(content);")
    for rowid, content_hash, deleted in ((1, "h1", None), (2, "h2", 123.0)):
        con.execute("INSERT INTO memories(rowid, content, tags, created_at_iso, content_hash, "
                    "metadata, deleted_at) VALUES (?,?,?,?,?,?,?)",
                    (rowid, "样板文本 测试", "", "2026-01-01", content_hash, "{}", deleted))
        con.execute("INSERT INTO memory_content_fts(rowid, content) VALUES (?,?)",
                    (rowid, "样板文本 测试"))
    con.commit()
    con.close()

    from aml.retrieval import Retriever
    cfg = cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})
    hits = Retriever(cfg).keyword("样板文本")
    assert [h["content_hash"] for h in hits] == ["h1"]
