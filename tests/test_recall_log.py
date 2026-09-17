"""召回账本的测试：记账、去重、裁剪、坏行容忍，以及与检索的联动。

要钉死的口径：
  · 账本只记 hash/字数/阶段/查询，**正文不落盘**（账本可能被分享）
  · 记账失败不能拖垮检索（记忆层是锦上添花，不是单点）
  · `--last N` 取的是**最近 N 次检索**里出现过的 hash，重复的只算一次
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import config as cfgmod  # noqa: E402
from aml import recall_log  # noqa: E402
from aml.retrieval import Retriever  # noqa: E402


def make_cfg(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["skill_roots"] = []
    return cfg


class FakeClient:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, n_results=10):
        return list(self.hits)


def memory(hash_, content="一条经验"):
    return {"content_hash": hash_, "content": content, "tags": ["kind:knowledge"],
            "created_at_iso": "2026-09-01T00:00:00Z", "metadata": {}}


def test_record_and_tail_roundtrip(tmp_path):
    cfg = make_cfg(tmp_path)
    recall_log.record(cfg, "P2", "查询一", ["h1", "h2"], chars=42, project="proj")
    recall_log.record(cfg, "P2", "查询二", ["h3"], chars=7)
    events = recall_log.tail(cfg, 1)
    assert len(events) == 1 and events[0]["query"] == "查询二"      # 新→旧
    assert events[0]["hashes"] == ["h3"]
    assert recall_log.tail(cfg, 5)[-1]["query"] == "查询一"


def test_log_never_contains_content(tmp_path):
    cfg = make_cfg(tmp_path)
    recall_log.record(cfg, "P2", "q", ["h1"], chars=10)
    raw = recall_log.log_path(cfg).read_text(encoding="utf-8")
    assert "content" not in raw and "正文" not in raw


def test_resolve_dedupes_across_events(tmp_path):
    cfg = make_cfg(tmp_path)
    recall_log.record(cfg, "P2", "旧", ["h1", "h2"], chars=1)
    recall_log.record(cfg, "P2", "新", ["h2", "h3"], chars=1)
    targets = recall_log.resolve(cfg, 1)
    assert list(targets) == ["h2", "h3"]
    both = recall_log.resolve(cfg, 2)
    assert list(both) == ["h2", "h3", "h1"]        # 最近优先
    assert both["h2"]["query"] == "新" and both["h1"]["query"] == "旧"


def test_broken_lines_are_skipped(tmp_path):
    cfg = make_cfg(tmp_path)
    recall_log.record(cfg, "P2", "好行", ["h1"], chars=1)
    with open(recall_log.log_path(cfg), "a", encoding="utf-8") as f:
        f.write("{这不是 JSON\n")
    assert len(recall_log.read_all(cfg)) == 1
    assert recall_log.resolve(cfg, 5) != {}


def test_record_failure_is_swallowed(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)

    def boom(*_a, **_k):
        raise OSError("磁盘满了")

    monkeypatch.setattr(recall_log, "log_path", boom)
    assert recall_log.record(cfg, "P2", "q", ["h1"]) is None      # 不抛


def test_prune_keeps_recent_only(tmp_path):
    cfg = make_cfg(tmp_path)
    for i in range(6):
        recall_log.record(cfg, "P2", f"q{i}", [f"h{i}"], chars=1)
    assert recall_log.prune(cfg, keep=2) == 4
    kept = recall_log.read_all(cfg)
    assert [e["query"] for e in kept] == ["q4", "q5"]


def test_render_lists_hashes_and_next_step(tmp_path):
    cfg = make_cfg(tmp_path)
    recall_log.record(cfg, "P2", "怎么修 GBK 崩溃", ["h1"], chars=12)
    text = recall_log.render(recall_log.tail(cfg, 1))
    assert "h1" in text and "aml feedback --last 1" in text
    assert "空的" in recall_log.render([])


def test_retriever_writes_ledger_with_hashes(tmp_path):
    cfg = make_cfg(tmp_path)
    hits = [(0.9, memory("h1")), (0.88, memory("h2"))]
    result = Retriever(cfg, client=FakeClient(hits)).search("随便问问", phase="P2")
    assert result.hashes == ["h1", "h2"]
    events = recall_log.tail(cfg, 1)
    assert events[0]["hashes"] == ["h1", "h2"] and events[0]["phase"] == "P2"
    assert events[0]["chars"] == result.used


def test_retriever_can_turn_the_ledger_off(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.data["retrieval"]["log_recalls"] = False
    Retriever(cfg, client=FakeClient([(0.9, memory("h1"))])).search("q", phase="P2")
    assert recall_log.read_all(cfg) == []


def test_ledger_failure_does_not_break_search(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("账本炸了")

    monkeypatch.setattr(recall_log, "record", boom)
    result = Retriever(cfg, client=FakeClient([(0.9, memory("h1"))])).search("q", phase="P2")
    assert result.lines and result.hashes == ["h1"]
    assert json.dumps(cfg.data)      # 配置没被记账搞坏
