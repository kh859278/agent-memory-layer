"""蒸馏队列的 `--session` 过滤不能动到别的待蒸记录。

2026-11-20 实测事故：`drain(session_id=...)` 把过滤后的列表写回 queue.data 并保存，
一次 `--session <id>` 就把另外 596 条 pending 全删了（已从 backups/distill_queue.json 恢复）。
这里钉住两件事：
  ① 只蒸指定会话时，其他 pending 原样保留；
  ② `--session` 接受全 id 或前 8 位短键（帮助里写的是“短 id”）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import distill  # noqa: E402


def _cfg(tmp_path):
    return cfgmod.load({"aml_home": str(tmp_path)})


def _write_queue(cfg, items):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.state_dir / "distill_queue.json"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"pending": items, "done": []}, f, ensure_ascii=False)
    return path


def _read_queue(cfg):
    with open(cfg.state_dir / "distill_queue.json", encoding="utf-8") as f:
        return json.load(f)


def _items():
    return [
        {"session_id": "session-aaaaaaaa-1111", "short": "aaaaaaaa", "queued_at": "2026-11-01T00:00:00"},
        {"session_id": "session-bbbbbbbb-2222", "short": "bbbbbbbb", "queued_at": "2026-11-02T00:00:00"},
        {"session_id": "session-cccccccc-3333", "short": "cccccccc", "queued_at": "2026-11-03T00:00:00"},
    ]


def _patch(monkeypatch, mems=None):
    mems = mems or [(1700000000, "做个事", "task"), (1700000001, "做完了", "reply")]
    monkeypatch.setattr(distill, "session_memories", lambda *a, **k: mems)
    monkeypatch.setattr(distill, "write_entries", lambda *a, **k: 0)
    monkeypatch.setattr(distill, "sink_markdown", lambda *a, **k: 0)


def test_session_filter_keeps_other_pending(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_queue(cfg, _items())
    _patch(monkeypatch)

    out = distill.drain(cfg, session_id="aaaaaaaa", client=object(),
                        call=lambda *a, **k: ("[]", {}), log=lambda *a: None)

    q = _read_queue(cfg)
    assert out["done"] == 1
    assert [x["short"] for x in q["pending"]] == ["bbbbbbbb", "cccccccc"], "别的待蒸记录不能被过滤掉"
    assert q["done"][0]["short"] == "aaaaaaaa"


def test_session_filter_accepts_full_id(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_queue(cfg, _items())
    _patch(monkeypatch)

    distill.drain(cfg, session_id="session-bbbbbbbb-2222", client=object(),
                  call=lambda *a, **k: ("[]", {}), log=lambda *a: None)
    assert [x["short"] for x in _read_queue(cfg)["pending"]] == ["aaaaaaaa", "cccccccc"]


def test_unknown_session_does_not_touch_queue(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_queue(cfg, _items())
    _patch(monkeypatch)
    logs = []

    out = distill.drain(cfg, session_id="deadbeef", client=object(),
                        call=lambda *a, **k: ("[]", {}), log=logs.append)
    assert out["done"] == 0
    assert len(_read_queue(cfg)["pending"]) == 3
    assert any("没有匹配" in x for x in logs)
