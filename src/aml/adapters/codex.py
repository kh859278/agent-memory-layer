"""Codex CLI 会话适配器。

`~/.codex/sessions/**/rollout-<时间戳>-<uuid>.jsonl`，一行一个事件：
顶层 `type` ∈ {session_meta, response_item, event_msg, ...}；
正文在 `response_item.payload{type:"message", role, content[]}`，
`session_meta.payload` 里有 `session_id` 与 `cwd`。

实测格式依据：2026-09-27 本机 codex-cli 0.157.1 生成的会话文件。
"""
from __future__ import annotations

import glob
import json
import os

from .. import text
from .base import Adapter, Turn


class CodexAdapter(Adapter):
    name = "codex"

    def discover(self):
        if not self.enabled:
            return []
        root = os.path.expanduser(self.options.get("sessions_dir") or "~/.codex/sessions")
        return [p for p in glob.glob(os.path.join(root, "**", "rollout-*.jsonl"), recursive=True)
                if self.want(p)]

    def turns(self, path):
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
                payload = j.get("payload") if isinstance(j.get("payload"), dict) else {}
                kind = j.get("type")
                if kind == "session_meta":
                    session = payload.get("session_id") or session
                    cwd = payload.get("cwd") or cwd
                    ts = payload.get("timestamp") or j.get("timestamp") or ts
                    continue
                if kind != "response_item" or payload.get("type") != "message":
                    continue
                role = payload.get("role")
                texts = [p.get("text", "") for p in (payload.get("content") or [])
                         if isinstance(p, dict) and p.get("text")]
                body = "\n".join(x for x in texts if x).strip()
                if role == "user":
                    # 环境上下文（<environment_context> 等）不是用户说的话
                    if not body or body.startswith("<"):
                        continue
                    if user is not None:
                        yield Turn("codex", session, text.project_of(cwd), text.iso(ts),
                                   user, "\n".join(reply), tools, path)
                    user, reply, tools, ts = body, [], [], j.get("timestamp") or ts
                elif role == "assistant":
                    if body:
                        reply.append(body)
        if user is not None:
            yield Turn("codex", session, text.project_of(cwd), text.iso(ts),
                       user, "\n".join(reply), tools, path)
