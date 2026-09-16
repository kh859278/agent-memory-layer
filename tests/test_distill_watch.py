"""蒸馏与监听里的纯逻辑测试（不调真实 LLM、不碰真实会话）。"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import distill, kb, watch  # noqa: E402

# ------------------------------------------------------------------- distill

def test_parse_entries_handles_code_fences_and_prose():
    raw = "好的，结果如下：\n```json\n[{\"title\":\"t\",\"body\":\"b\"}]\n```\n希望有帮助"
    assert distill.parse_entries(raw) == [{"title": "t", "body": "b"}]
    assert distill.parse_entries("没有可复用知识") == []
    assert distill.parse_entries("[not json") == []


def test_review_after_days_depend_on_type():
    base = dt.datetime(2026, 1, 1)
    assert distill.review_after_for("pitfall", base) == "2026-06-30"      # 180 天
    assert distill.review_after_for("decision", base) == "2028-01-01"     # 730 天
    assert distill.review_after_for("未知类型", base) == "2027-01-01"      # 默认 365 天


def test_build_transcript_truncates_per_memory_and_total():
    mems = [(1700000000 + i, "内容" * 300, "task" if i % 2 else "reply") for i in range(60)]
    text, used = distill.build_transcript(mems, max_chars=800, per_mem=50)
    assert len(text) <= 800
    assert used < len(mems)                 # 超长时抽样，不是全塞进去
    assert text.startswith("[")             # 仍带原始时间戳


def test_queue_dedup_and_incremental_requeue(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    queue = distill.Queue(cfg)
    assert queue.enqueue("session-a", agent="dsh", mem_count=10) is True
    assert queue.enqueue("session-a", agent="dsh", mem_count=10) is False     # 已在队列里

    # 蒸完之后再入队：内容没怎么涨 → 不重蒸
    queue.data["pending"] = []
    queue.data["done"] = [{"session_id": "session-a", "short": "a", "mem_count": 10}]
    queue.save()
    assert queue.enqueue("session-a", mem_count=11) is False
    # 涨得够多（≥+5 且 ≥1.5 倍）→ 增量重蒸
    assert queue.enqueue("session-a", mem_count=20) is True
    assert queue.data["pending"][0]["reason"].startswith("增量重蒸")


# --------------------------------------------------------------------- watch

def test_cooled_only_returns_new_and_quiet_files():
    now = 1_700_000_000
    files = {
        "a.jsonl": (100, now - 300),      # 静默够久 → 该处理
        "b.jsonl": (100, now - 10),       # 还在写 → 等下一轮
        "c.jsonl": (100, now - 300),      # 静默够久，但签名没变 → 跳过
    }
    known = {"c.jsonl": {"size": 100, "mtime": now - 300}}
    assert watch.cooled(files, known, stable_seconds=120, now=now) == ["a.jsonl"]


def test_should_distill_policies_per_agent():
    assert watch.should_distill(r"C:\x\.claude\projects\p\abc.jsonl")["distill"] is True
    assert watch.should_distill(r"C:\x\.claude\projects\p\subagents\s.jsonl")["distill"] is False
    assert watch.should_distill(r"C:\x\kimi\s1\wire.jsonl")["distill"] is False      # kimi 只入库
    dsh = watch.should_distill(r"C:\x\.dsh\sessions\job\session-abc\session.v3.jsonl.zstd")
    assert dsh["distill"] is True and dsh["min_tasks"] == 3                         # 挡 1–2 轮子代理
    assert watch.should_distill("/tmp/random.txt")["distill"] is False


def test_watch_state_roundtrip(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    state = watch.WatchState(cfg)
    state.data["files"]["x"] = {"size": 1, "mtime": 2}
    state.data["archived"] = ["session-1"]
    state.save()
    again = watch.WatchState(cfg)
    assert again.data["files"]["x"]["size"] == 1
    assert again.data["archived"] == ["session-1"]


# ------------------------------------------------------------------------ kb

def test_chunks_overlap_and_bounds():
    text = "字" * 700
    parts = kb.chunks(text, size=280, overlap=40)
    assert len(parts) == 3
    assert all(len(p) <= 280 for p in parts)
    assert kb.chunks("") == []
    assert kb.chunks("短") == ["短"]


def test_build_index_writes_file_with_counts(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    (cfg.knowledge_dir / "源知识").mkdir(parents=True)
    (cfg.knowledge_dir / "源知识" / "a.md").write_text("内容", encoding="utf-8")
    memories = [
        {"tags": ["kind:knowledge", "domain:env-windows"], "content": "【坑】一条经验",
         "metadata": {"domain": "env-windows", "title": "坑"}, "created_at_iso": "2026-01-01T00:00:00Z"},
        {"tags": ["project:demo", "kb:源知识"], "content": "x", "metadata": {},
         "created_at_iso": "2026-01-01T00:00:00Z"},
    ]
    info = kb.build_index(cfg, memories=memories)
    text = (cfg.knowledge_dir / "索引.md").read_text(encoding="utf-8")
    assert info["memories"] == 2 and info["knowledge"] == 1
    assert "沉淀层" in text and "env-windows" in text and "`demo`" in text
    assert "源知识" in text


def test_ingest_docs_dry_run_counts(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    (cfg.knowledge_dir / "源知识").mkdir(parents=True)
    (cfg.knowledge_dir / "源知识" / "a.md").write_text("一" * 600, encoding="utf-8")
    (cfg.knowledge_dir / "源知识" / "skip.bin").write_text("x", encoding="utf-8")
    info = kb.ingest_docs(cfg, dirs=["源知识"], dry_run=True)
    assert info["files"] == 1
    assert info["chunks"] == 3


def test_queue_file_is_valid_json(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    queue = distill.Queue(cfg)
    queue.enqueue("session-x", workspace="demo", agent="claude-code", mem_count=7)
    data = json.loads((cfg.state_dir / "distill_queue.json").read_text(encoding="utf-8"))
    assert data["pending"][0]["short"] == "x"
    assert data["pending"][0]["mem_count"] == 7
