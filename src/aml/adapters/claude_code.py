"""Claude Code 会话适配器。

`~/.claude/projects/**/*.jsonl`，一行一个事件；子代理记录在 `subagents/` 子目录里，
单独打标签（`claude-code:subagent`），因为它们的粒度太细、蒸馏价值低。
"""
from __future__ import annotations

import glob
import json
import os

from .. import text
from .base import Adapter, Turn


class ClaudeCodeAdapter(Adapter):
    name = "claude_code"

    def discover(self):
        if not self.enabled:
            return []
        root = os.path.expanduser(self.options.get("projects_dir") or "~/.claude/projects")
        return [p for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
                if self.want(p)]

    def turns(self, path):
        agent = "claude-code:subagent" if "subagents" in path.replace("\\", "/") else "claude-code"
        session = os.path.basename(path)[:-6]
        user, reply, tools, ts, cwd = None, [], [], None, None
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    j = json.loads(raw)
                except ValueError:
                    continue
                kind = j.get("type")
                cwd = j.get("cwd") or cwd
                if kind == "user":
                    content = (j.get("message") or {}).get("content")
                    texts = []
                    if isinstance(content, str):
                        texts.append(content)
                    elif isinstance(content, list):
                        texts += [p.get("text", "") for p in content
                                  if isinstance(p, dict) and p.get("type") == "text"]
                    body = "\n".join(x for x in texts if x).strip()
                    # 工具结果与系统注入都不是"用户说的话"
                    if not body or body.startswith("<") or "system-reminder" in body[:80]:
                        continue
                    if user is not None:
                        yield Turn(agent, session, text.project_of(cwd), text.iso(ts),
                                   user, "\n".join(reply), tools, path)
                    user, reply, tools, ts = body, [], [], j.get("timestamp")
                elif kind == "assistant":
                    for part in ((j.get("message") or {}).get("content") or []):
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "text":
                            reply.append(part.get("text", ""))
                        elif part.get("type") == "tool_use":
                            tools.append(part.get("name", "?"))
        if user is not None:
            yield Turn(agent, session, text.project_of(cwd), text.iso(ts),
                       user, "\n".join(reply), tools, path)
