"""适配器解析测试。

fixture 全部是**测试里现场生成的合成数据**（不是真实会话），所以仓库里不存在任何用户内容。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml.adapters.claude_code import ClaudeCodeAdapter  # noqa: E402
from aml.adapters.kimi import KimiAdapter  # noqa: E402


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# --------------------------------------------------------------- Claude Code

def test_claude_code_turn_extraction(tmp_path):
    rows = [
        {"type": "user", "timestamp": "2026-08-01T10:00:00Z", "cwd": r"C:\work\proj-a",
         "message": {"content": "帮我修一下 powershell 脚本的中文乱码"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "根因是 GBK 解码。"},
            {"type": "tool_use", "name": "Edit"}]}},
        # 系统注入：不是用户说的话，必须跳过
        {"type": "user", "timestamp": "2026-08-01T10:05:00Z", "cwd": r"C:\work\proj-a",
         "message": {"content": "<system-reminder>noise</system-reminder>"}},
    ]
    path = write_jsonl(str(tmp_path / "projects" / "p" / "sess-1.jsonl"), rows)
    adapter = ClaudeCodeAdapter({"enabled": True}, {"max_len": 300})
    turns = list(adapter.turns(path))

    assert len(turns) == 1
    turn = turns[0]
    assert turn.agent == "claude-code"
    assert turn.session == "sess-1"
    assert turn.project == "proj-a"
    assert turn.ts == "2026-08-01T10:00:00Z"
    assert "乱码" in turn.user
    assert "GBK" in turn.reply
    assert turn.tools == ["Edit"]


def test_claude_code_subagent_tagged_separately(tmp_path):
    path = write_jsonl(str(tmp_path / "projects" / "p" / "subagents" / "s2.jsonl"),
                       [{"type": "user", "timestamp": "2026-08-02T00:00:00Z",
                         "message": {"content": "子代理里的一条消息"}}])
    adapter = ClaudeCodeAdapter({"enabled": True}, {})
    assert list(adapter.turns(path))[0].agent == "claude-code:subagent"


def test_claude_code_discover_filters(tmp_path):
    root = tmp_path / "projects"
    write_jsonl(str(root / "a" / "one.jsonl"), [])
    write_jsonl(str(root / "b" / "two.jsonl"), [])
    adapter = ClaudeCodeAdapter({"enabled": True, "projects_dir": str(root)}, {})
    assert len(adapter.discover()) == 2
    only = os.path.abspath(str(root / "a" / "one.jsonl"))
    adapter2 = ClaudeCodeAdapter({"enabled": True, "projects_dir": str(root)}, {}, only_files=[only])
    assert adapter2.discover() == [only]


# ---------------------------------------------------------------------- Kimi

def test_kimi_skips_internal_title_prompts(tmp_path):
    rows = [
        {"type": "turn.prompt", "time": 1755000000000,
         "input": [{"type": "text", "text": "解释这个报错\n<meta name=\"x\"/>"}]},
        {"type": "context.append_message",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "报错是这样来的。"}]}},
        {"type": "turn.prompt", "time": 1755000100000,
         "input": [{"type": "text", "text": "Generate a concise title for this conversation"}]},
    ]
    path = write_jsonl(str(tmp_path / "sessions" / "s1" / "wire.jsonl"), rows)
    adapter = KimiAdapter({"enabled": True, "sessions_dir": str(tmp_path / "sessions")}, {})
    turns = list(adapter.turns(path))

    assert len(turns) == 1
    assert turns[0].agent == "kimi-code"
    assert "<meta" not in turns[0].user          # 内部标签要剥掉
    assert turns[0].reply == "报错是这样来的。"


# ------------------------------------------------------------------- zstd/DSH

def test_dsh_adapter_parses_when_zstandard_available(tmp_path):
    zstd = pytest.importorskip("zstandard")
    rows = [
        {"type": "session", "cwd": r"C:\work\proj-b", "createdAt": "2026-08-03T01:00:00Z"},
        {"type": "user/message", "time": "2026-08-03T01:00:05Z",
         "data": {"content": [{"type": "text", "text": "给这个函数加测试"}]}},
        {"type": "assistant/message",
         "data": {"message": {"content": [{"type": "text", "text": "先写失败用例。"},
                                          {"type": "tool-call", "name": "write"}]}}},
    ]
    raw = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows).encode()
    path = tmp_path / "sessions" / "session-abc" / "session.v3.jsonl.zstd"
    path.parent.mkdir(parents=True)
    path.write_bytes(zstd.ZstdCompressor().compress(raw))

    from aml.adapters.dsh import DshAdapter
    adapter = DshAdapter({"enabled": True, "sessions_dir": str(tmp_path / "sessions")}, {})
    turns = list(adapter.turns(str(path)))
    assert len(turns) == 1
    assert turns[0].session == "abc"
    assert turns[0].project == "proj-b"
    assert turns[0].tools == ["write"]
