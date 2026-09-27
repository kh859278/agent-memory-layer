"""Qwen Code 会话适配器（Gemini CLI 的 fork）。

`~/.qwen/projects/<项目 slug>/chats/<uuid>.jsonl`，一行一个事件：
`type` ∈ {user, assistant, system}，正文在 `message.parts[]` 的 `text`，
工具调用在 `parts[].functionCall.name`；时间戳是 ISO 串，`cwd` 在每行顶层。

实测格式依据：2026-09-27 本机 @qwen-code/qwen-code 0.24.6 生成的会话文件。
Gemini CLI 是同源 fork，格式相近（未实测，故本适配器只覆盖 ~/.qwen）。
"""
from __future__ import annotations

import glob
import json
import os

from .. import text
from .base import Adapter, Turn


class QwenAdapter(Adapter):
    name = "qwen_code"

    def discover(self):
        if not self.enabled:
            return []
        root = os.path.expanduser(self.options.get("projects_dir") or "~/.qwen/projects")
        return [p for p in glob.glob(os.path.join(root, "*", "chats", "*.jsonl"))
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
                cwd = j.get("cwd") or cwd
                kind = j.get("type")
                parts = ((j.get("message") or {}).get("parts") or [])
                texts = [p.get("text", "") for p in parts
                         if isinstance(p, dict) and p.get("text")]
                body = "\n".join(x for x in texts if x).strip()
                called = [p.get("functionCall", {}).get("name") for p in parts
                          if isinstance(p, dict) and isinstance(p.get("functionCall"), dict)]
                if kind == "user":
                    if not body or body.startswith("<"):
                        continue
                    if user is not None:
                        yield Turn("qwen-code", session, text.project_of(cwd), text.iso(ts),
                                   user, "\n".join(reply), tools, path)
                    user, reply, tools, ts = body, [], [], j.get("timestamp")
                elif kind == "assistant":
                    if body:
                        reply.append(body)
                    tools += [x for x in called if x]
        if user is not None:
            yield Turn("qwen-code", session, text.project_of(cwd), text.iso(ts),
                       user, "\n".join(reply), tools, path)
