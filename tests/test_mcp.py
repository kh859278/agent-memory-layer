"""MCP server 测试：协议握手、工具列表、工具调用（全部用假对象，不联网）。

MCP 的 stdio 传输就是"一行一个 JSON-RPC"，所以测试直接喂 message 给 Server.handle()，
以及用 io.StringIO 模拟 stdio 主循环——不需要真的起进程。
"""
from __future__ import annotations

import io
import json
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import config as cfgmod  # noqa: E402
from aml.mcp_server import Server, serve  # noqa: E402


class FakeResult:
    def __init__(self, lines, diag=None, budget=600):
        self.lines = lines
        self.diag = diag or {}
        self.budget = budget
        self.used = sum(len(x) for x in lines)

    @property
    def empty(self):
        return not self.lines

    def render(self):
        return "\n".join(self.lines) if self.lines else "（没命中）"


class FakeRetriever:
    def __init__(self, lines=None):
        self.calls = []
        self.lines = lines if lines is not None else ["[沉淀|env-windows|2026-09-13|0.85] 一条经验"]

    def search(self, query, phase="P2", project=None, tag=None, n=None, allow_repeat=False,
               include_procedure=False):
        self.calls.append({"query": query, "phase": phase, "project": project, "tag": tag, "n": n,
                           "allow_repeat": allow_repeat, "include_procedure": include_procedure})
        return FakeResult(list(self.lines))


class FakeClient:
    def __init__(self):
        self.stored = []

    def store(self, content, tags=None, metadata=None, conversation_id=None):
        self.stored.append({"content": content, "tags": tags, "metadata": metadata,
                            "conversation_id": conversation_id})
        return {"success": True}


class FakeNotices:
    def __init__(self, text=""):
        self.text = text
        self.acked = 0

    def brief(self, limit=None):
        return self.text

    def ack(self, notice_id=None, all_=False):
        self.acked += 1
        return 1


def make_server(tmp_path, **kwargs):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    kwargs.setdefault("retriever", FakeRetriever())
    kwargs.setdefault("client", FakeClient())
    kwargs.setdefault("notices", FakeNotices())
    kwargs.setdefault("doctor", lambda cfg: [])
    return Server(cfg, **kwargs)


def test_initialize_and_tools_list(tmp_path):
    server = make_server(tmp_path)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "aml"
    assert init["result"]["protocolVersion"]

    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert {"search", "store", "phase_spec", "brief", "ack", "doctor"} <= set(names)
    for tool in listed["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_unknown_method_and_notification(tmp_path):
    server = make_server(tmp_path)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    err = server.handle({"jsonrpc": "2.0", "id": 9, "method": "no/such"})
    assert err["error"]["code"] == -32601


def test_search_tool_passes_phase_and_renders(tmp_path):
    retriever = FakeRetriever()
    server = make_server(tmp_path, retriever=retriever)
    response = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "search",
                                         "arguments": {"query": "编码", "phase": "P3",
                                                       "tag": "domain:env-windows"}}})
    payload = response["result"]
    assert payload["isError"] is False
    assert "一条经验" in payload["content"][0]["text"]
    assert retriever.calls[0]["phase"] == "P3"
    assert retriever.calls[0]["tag"] == "domain:env-windows"


def test_store_tool_layers_knowledge_vs_project(tmp_path):
    """分层是硬规则：给 project 就是项目事实，不打 kind:knowledge；否则进沉淀层带复核期。"""
    client = FakeClient()
    server = make_server(tmp_path, client=client)

    # 项目专属事实
    server.call_tool("store", {"content": "这个项目的客户叫 X", "project": "demo"})
    project_save = client.stored[-1]
    assert project_save["tags"] == ["project:demo"]
    assert "review_after" not in project_save["metadata"]

    # 跨项目可复用经验（无 project）
    server.call_tool("store", {"content": "一条可复用经验", "ktype": "pitfall", "title": "标题"})
    knowledge_save = client.stored[-1]
    assert "kind:knowledge" in knowledge_save["tags"]
    assert "reusable:true" in knowledge_save["tags"]
    assert knowledge_save["metadata"]["review_after"]        # pitfall 有复核期
    assert knowledge_save["conversation_id"] == "mcp-store"


def test_brief_and_ack_tools(tmp_path):
    notices = FakeNotices("📌 技能自动更新 2 个")
    server = make_server(tmp_path, notices=notices)
    text = server.call_tool("brief", {})
    assert text.startswith("📌")
    assert "已标记" in server.call_tool("ack", {})
    assert notices.acked == 1

    empty = make_server(tmp_path, notices=FakeNotices(""))
    assert "不要" in empty.call_tool("brief", {})   # 没变化时明确提示"别在回答里提"


def test_phase_spec_lists_budget(tmp_path):
    server = make_server(tmp_path)
    spec = server.call_tool("phase_spec", {})
    assert "P2" in spec and "预算" in spec
    one = server.call_tool("phase_spec", {"phase": "P2"})
    assert "P2" in one and "P0" not in one


def test_tool_error_is_returned_not_raised(tmp_path):
    server = make_server(tmp_path)
    response = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "不存在的工具", "arguments": {}}})
    assert response["result"]["isError"] is True
    assert "未知工具" in response["result"]["content"][0]["text"]


def test_stdio_loop_handles_requests_and_garbage(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    lines = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        "这不是 JSON",
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]) + "\n"
    out = io.StringIO()
    serve(cfg, stdin=io.StringIO(lines), stdout=out)      # 这条路径不碰网络（没有工具调用）
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "aml"
    assert responses[1]["error"]["code"] == -32700        # 坏 JSON 有明确错误码
    assert responses[2]["id"] == 2 and responses[2]["result"]["tools"]
