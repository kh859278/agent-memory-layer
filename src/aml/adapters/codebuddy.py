"""WorkBuddy 会话适配器（腾讯 codebuddy，Claude Code 的 fork）。

`~/.codebuddy/projects/<项目 slug>/<uuid>.jsonl`，一行一个事件：
`type == "message"` 的行带 `role`（user / assistant），正文在 `content[]`，
part 的 `type` 是 `input_text` / `output_text`；时间戳是**毫秒整数**。
目录布局与 Claude Code 同构，但字段名不同，所以单独一个模块而不是复用 claude_code。

实测格式依据：2026-09-27 本机 @tencent-ai/codebuddy-code 2.158.0 生成的会话文件。
"""
from __future__ import annotations

import glob
import json
import os

from .. import text
from .base import Adapter, Turn


class WorkBuddyAdapter(Adapter):
    name = "workbuddy"

    def discover(self):
        if not self.enabled:
            return []
        root = os.path.expanduser(self.options.get("projects_dir") or "~/.codebuddy/projects")
        return [p for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
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
                if j.get("type") != "message":
                    continue
                role = j.get("role")
                texts = [p.get("text", "") for p in (j.get("content") or [])
                         if isinstance(p, dict) and p.get("text")]
                body = "\n".join(x for x in texts if x).strip()
                if role == "user":
                    if not body or body.startswith("<"):
                        continue
                    if user is not None:
                        yield Turn("workbuddy", session, text.project_of(cwd), text.iso(ts),
                                   user, "\n".join(reply), tools, path)
                    user, reply, tools, ts = body, [], [], j.get("timestamp")
                elif role == "assistant":
                    if body:
                        reply.append(body)
        if user is not None:
            yield Turn("workbuddy", session, text.project_of(cwd), text.iso(ts),
                       user, "\n".join(reply), tools, path)
