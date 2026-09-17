"""MCP server：把记忆层的能力暴露给任何 MCP 客户端（stdio + JSON-RPC 2.0）。

为什么不用官方 SDK：这套东西的定位是"装完就能用"，而 MCP 的 stdio 传输就是
**一行一个 JSON-RPC 消息**，自己实现只要 100 行，省掉一个依赖（也就省掉版本冲突）。

暴露的工具（都在 `tools/list` 里带 JSON Schema）：
  · `search`      分阶段检索（P0–P6 + 级联回退 + 未命中解释）—— 见 docs/PROTOCOL.md
  · `store`       写回一条记忆（agent 在任务收尾时留下结论）
  · `phase_spec`  查某个阶段的意图与预算（agent 自己决定该给多少上下文）
  · `brief`       取巡检攒下的「这次新增/更新了什么」（≤100 字）
  · `ack`         确认已经把 brief 念给用户了
  · `doctor`      体检摘要

接线（客户端侧）：
  · Claude Code:  `claude mcp add aml -- aml mcp`
  · DSH:          在 profile 里加一条 stdio MCP 客户端，命令 `aml mcp`
  · 任何客户端:    command=aml, args=["mcp"]，传输 stdio
"""
from __future__ import annotations

import json
import sys

from . import __version__
from .doctor import render as doctor_render
from .doctor import run as doctor_run
from .http import MemoryClient
from .retrieval import Retriever
from .text import ensure_utf8_stdio

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aml"

TOOLS = [
    {
        "name": "search",
        "description": ("分阶段检索记忆层。阶段决定条数与字数预算，未命中会解释为什么空。"
                        "P0 接到任务 / P1 定方案 / P2 动手前 / P3 报错后 / P4 决策 / P5 交付前。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查什么（用具体名词，别写整句）"},
                "phase": {"type": "string", "enum": ["P0", "P1", "P2", "P3", "P4", "P5", "P6"],
                          "description": "默认 P2（动手前）"},
                "project": {"type": "string", "description": "优先返回该项目的历史"},
                "tag": {"type": "string", "description": "收窄标签，如 domain:env-windows"},
                "n": {"type": "integer", "description": "覆盖该阶段的条数上限"},
                "allow_repeat": {"type": "boolean",
                                 "description": "同一阶段同一查询默认 10 分钟内不重查；"
                                                "确实需要重查时传 true（否则返回的是「冷却跳过」而不是空库说明）"},
                "include_procedure": {"type": "boolean",
                                      "description": "默认跳过「程序性内容」（技能正文等、照做会改变行为的"
                                                     "指令）；确实要看时传 true"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "store",
        "description": "写回一条记忆。可跨项目复用的经验请带 kind:knowledge + domain:<领域> 标签。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "自包含的正文（脱离本项目也看得懂）"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "如 ['kind:knowledge','reusable:true','domain:env-windows']"},
                "title": {"type": "string", "description": "短标题（写入 metadata）"},
                "ktype": {"type": "string",
                          "description": "pitfall|pattern|decision|tooling|checklist，决定复核期"},
                "project": {"type": "string", "description": "只对本项目成立时用 project:<名>"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "phase_spec",
        "description": "查阶段定义：什么时候用、要回答什么、条数与字数预算。",
        "inputSchema": {"type": "object", "properties": {
            "phase": {"type": "string", "description": "留空返回全部阶段"}}},
    },
    {
        "name": "brief",
        "description": "取一句 ≤100 字的「技能/依赖这几轮新增或更新了什么」；没有变化时返回空串。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ack",
        "description": "确认 brief 已经念给用户了（不 ack 会被下次回答重复播报）。",
        "inputSchema": {"type": "object", "properties": {
            "all": {"type": "boolean", "description": "默认 true，标记全部未播报条目"}}},
    },
    {
        "name": "doctor",
        "description": "记忆层体检摘要：服务、向量覆盖、FTS、索引新鲜度、适配器、检索自测。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Server:
    """协议与工具实现的分离点：handle() 是纯函数式的，方便测试（不需要 stdio）。"""

    def __init__(self, cfg, retriever=None, client=None, notices=None, doctor=doctor_run):
        self.cfg = cfg
        self._retriever = retriever
        self._client = client
        self._notices = notices
        self._doctor = doctor

    # ---- 懒加载（测试里可以注入假对象，避免联网/落盘） ----
    @property
    def retriever(self) -> Retriever:
        if self._retriever is None:
            self._retriever = Retriever(self.cfg)
        return self._retriever

    @property
    def client(self) -> MemoryClient:
        if self._client is None:
            self._client = MemoryClient(self.cfg.api)
        return self._client

    @property
    def notices(self):
        if self._notices is None:
            from .patrol.notify import NoticeQueue
            self._notices = NoticeQueue(self.cfg)
        return self._notices

    # ---------------------------------------------------------------- 协议
    def handle(self, message: dict):
        """处理一条 JSON-RPC 消息。通知（无 id）返回 None，不产生响应。"""
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            return _ok(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return _ok(msg_id, {})
        if method == "tools/list":
            return _ok(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                text = self.call_tool(name, args)
            except Exception as e:  # noqa: BLE001 - 工具出错要回给模型，而不是崩掉服务
                return _ok(msg_id, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                                    "isError": True})
            return _ok(msg_id, {"content": [{"type": "text", "text": text}], "isError": False})
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}

    # ---------------------------------------------------------------- 工具
    def call_tool(self, name: str, args: dict) -> str:
        if name == "search":
            result = self.retriever.search(args["query"], phase=args.get("phase", "P2"),
                                           project=args.get("project"), tag=args.get("tag"),
                                           n=args.get("n"),
                                           allow_repeat=bool(args.get("allow_repeat", False)),
                                           include_procedure=bool(args.get("include_procedure", False)))
            extra = ""
            if not result.empty:
                extra = "\n\n（引用时请说明：来自记忆层，层级与日期见每行前缀）"
            return result.render() + extra

        if name == "store":
            tags = list(args.get("tags") or [])
            metadata = {}
            if args.get("title"):
                metadata["title"] = args["title"]
            # 分层是硬规则（见 docs/PROTOCOL.md）：给了 project 就是"项目专属事实"，
            # 不打 kind:knowledge；只有跨项目可复用的经验才进沉淀层、才有复核期。
            explicit_knowledge = any(t.startswith("kind:") for t in tags)
            if args.get("project") and not explicit_knowledge:
                tags.append(f"project:{args['project']}")
            else:
                if not explicit_knowledge:
                    tags.append("kind:knowledge")
                    tags.append("reusable:true")
                if args.get("ktype"):
                    metadata["ktype"] = args["ktype"]
                    from .distill import review_after_for
                    metadata["review_after"] = review_after_for(args["ktype"])
                if args.get("project"):
                    tags.append(f"project:{args['project']}")
            res = self.client.store(args["content"], tags, metadata,
                                    conversation_id="mcp-store")
            return ("已写入。" if res.get("success") else f"未写入（可能重复）：{res}") + \
                   f"\n标签：{', '.join(tags) or '（无）'}"

        if name == "phase_spec":
            wanted = (args.get("phase") or "").upper()
            phases = self.cfg.section("retrieval").get("phases") or {}
            rows = []
            for key, spec in phases.items():
                if wanted and key.upper() != wanted:
                    continue
                rows.append(f"{key}：{spec.get('intent', '')}\n"
                            f"    预算 ≤{spec.get('results')} 条 / ≤{spec.get('chars')} 字")
            if not rows:
                return f"没有这个阶段：{wanted}（或配置里没定义）"
            return "阶段定义（完整协议见 docs/PROTOCOL.md）：\n" + "\n".join(rows)

        if name == "brief":
            text = self.notices.brief()
            return text if text else "（没有新变化 —— 这种情况**不要**在回答里提更新）"

        if name == "ack":
            count = self.notices.ack(all_=bool(args.get("all", True)))
            return f"已标记 {count} 条为已播报。"

        if name == "doctor":
            checks = self._doctor(self.cfg)
            return doctor_render(checks)

        raise ValueError(f"未知工具：{name}")


def _ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(cfg, stdin=None, stdout=None) -> int:
    """stdio 主循环：一行一条 JSON-RPC。"""
    ensure_utf8_stdio()
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server(cfg)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                     "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue
        response = server.handle(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0


def main(argv=None) -> int:
    """`aml mcp` 的入口：读取配置后进入 stdio 循环。"""
    from .config import load
    return serve(load())


if __name__ == "__main__":
    raise SystemExit(main())
