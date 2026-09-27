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


# ------------------------------------------------------------------- Codex CLI

def test_codex_turn_extraction_and_env_skipped(tmp_path):
    rows = [
        {"timestamp": "2026-09-27T04:32:19Z", "type": "session_meta",
         "payload": {"session_id": "sess-codex-1", "cwd": r"C:\work\proj-c"}},
        # 环境上下文不是用户说的话
        {"timestamp": "2026-09-27T04:32:20Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}},
        {"timestamp": "2026-09-27T04:32:21Z", "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "这个报错怎么修"}]}},
        {"timestamp": "2026-09-27T04:32:22Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "先看栈顶。"}]}},
    ]
    path = write_jsonl(str(tmp_path / "sessions" / "2026" / "09" / "27"
                           / "rollout-2026-09-27T04-32-19-sess-codex-1.jsonl"), rows)
    from aml.adapters.codex import CodexAdapter
    adapter = CodexAdapter({"enabled": True, "sessions_dir": str(tmp_path / "sessions")}, {})
    turns = list(adapter.turns(path))

    assert len(turns) == 1
    assert turns[0].agent == "codex"
    assert turns[0].session == "sess-codex-1"       # 取自 session_meta
    assert turns[0].project == "proj-c"
    assert turns[0].ts == "2026-09-27T04:32:21Z"
    assert "报错" in turns[0].user
    assert turns[0].reply == "先看栈顶。"
    assert len(adapter.discover()) == 1


# ------------------------------------------------------------------- Qwen Code

def test_qwen_turn_extraction(tmp_path):
    rows = [
        {"uuid": "u1", "sessionId": "sess-qwen-1", "timestamp": "2026-09-27T04:37:42.702Z",
         "type": "user", "cwd": r"C:\work\proj-q",
         "message": {"role": "user", "parts": [{"text": "解释一下这个索引"}]}},
        {"uuid": "u2", "sessionId": "sess-qwen-1", "timestamp": "2026-09-27T04:37:44.543Z",
         "type": "assistant", "cwd": r"C:\work\proj-q",
         "message": {"role": "model", "parts": [{"text": "它是路由层。"},
                                                 {"functionCall": {"name": "read_file"}}]}},
    ]
    path = write_jsonl(str(tmp_path / "projects" / "slug" / "chats" / "sess-qwen-1.jsonl"), rows)
    from aml.adapters.qwen import QwenAdapter
    adapter = QwenAdapter({"enabled": True, "projects_dir": str(tmp_path / "projects")}, {})
    turns = list(adapter.turns(path))

    assert len(turns) == 1
    assert turns[0].agent == "qwen-code"
    assert turns[0].project == "proj-q"
    assert turns[0].reply == "它是路由层。"
    assert turns[0].tools == ["read_file"]


# ------------------------------------------------------------------- WorkBuddy

def test_workbuddy_ms_timestamp_and_content_parts(tmp_path):
    rows = [
        {"id": "m1", "timestamp": 1790486878475, "type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "帮我写周记"}],
         "sessionId": "sess-wb-1", "cwd": r"C:\work\proj-w"},
        {"timestamp": 1790486878496, "type": "file-history-snapshot"},   # 非 message 行要跳过
        {"id": "m2", "timestamp": 1790486878744, "type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "按上周的格式来。"}]},
    ]
    path = write_jsonl(str(tmp_path / "projects" / "slug" / "sess-wb-1.jsonl"), rows)
    from aml.adapters.codebuddy import WorkBuddyAdapter
    adapter = WorkBuddyAdapter({"enabled": True, "projects_dir": str(tmp_path / "projects")}, {})
    turns = list(adapter.turns(path))

    assert len(turns) == 1
    assert turns[0].agent == "workbuddy"
    assert turns[0].session == "sess-wb-1"
    assert turns[0].reply == "按上周的格式来。"
    # 毫秒整数必须被转成 ISO
    assert turns[0].ts.startswith("2026-") and turns[0].ts.endswith("Z")


# -------------------------------------------------------------------- OpenCode

def test_opencode_reads_sqlite(tmp_path):
    import sqlite3
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.executescript(
        "create table session(id text, directory text, time_created integer);"
        "create table message(id text, session_id text, time_created integer, data text);"
        "create table part(id text, message_id text, session_id text,"
        " time_created integer, data text);")
    con.execute("insert into session values (?,?,?)", ("sess-oc-1", r"C:\work\proj-o", 1790483517895))
    con.execute("insert into message values (?,?,?,?)",
                ("m1", "sess-oc-1", 1790483518107, json.dumps({"role": "user"})))
    con.execute("insert into message values (?,?,?,?)",
                ("m2", "sess-oc-1", 1790483519022, json.dumps({"role": "assistant"})))
    con.execute("insert into part values (?,?,?,?,?)",
                ("p1", "m1", "sess-oc-1", 1, json.dumps({"type": "text", "text": "会话存在哪里"})))
    con.execute("insert into part values (?,?,?,?,?)",
                ("p2", "m2", "sess-oc-1", 2, json.dumps({"type": "text", "text": "存在 sqlite 里。"})))
    con.commit()
    con.close()

    from aml.adapters.opencode import OpenCodeAdapter
    adapter = OpenCodeAdapter({"enabled": True, "db_path": str(db)}, {})
    assert adapter.discover() == [str(db)]
    turns = list(adapter.turns(str(db)))

    assert len(turns) == 1
    assert turns[0].agent == "opencode"
    assert turns[0].session == "sess-oc-1"
    assert turns[0].project == "proj-o"
    assert turns[0].user == "会话存在哪里"
    assert turns[0].reply == "存在 sqlite 里。"

