"""Kimi Code（kimi-desktop 内置 runtime）会话适配器。

路径（Windows）：`%APPDATA%\\kimi-desktop\\daimon-share\\daimon\\runtime\\kimi-code\\home\\sessions\\**\\wire.jsonl`
注意：Kimi 会把"生成标题"这类内部调用也写进 wire.jsonl，必须过滤，否则记忆全是噪声。
"""
from __future__ import annotations

import glob
import json
import os
import re

from .. import text
from .base import Adapter, Turn

ATTACHMENT = re.compile(r"<attachment>.*?</attachment>", re.S)
META_TAG = re.compile(r"<meta[^>]*/?>")
INTERNAL_PROMPTS = ("Generate a concise title",)


class KimiAdapter(Adapter):
    name = "kimi"

    def _root(self) -> str:
        raw = self.options.get("sessions_dir") or ""
        return os.path.expandvars(os.path.expanduser(raw))

    def discover(self):
        if not self.enabled:
            return []
        return [p for p in glob.glob(os.path.join(self._root(), "**", "wire.jsonl"), recursive=True)
                if self.want(p)]

    def turns(self, path):
        session = os.path.basename(os.path.dirname(os.path.dirname(path)))
        user, reply, ts, cwd = None, [], None, None
        try:
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
                    if kind == "turn.prompt":
                        body = "\n".join(p.get("text", "") for p in (j.get("input") or [])
                                         if isinstance(p, dict) and p.get("type") == "text")
                        body = META_TAG.sub("", ATTACHMENT.sub("", body)).strip()
                        if not body or any(k in body for k in INTERNAL_PROMPTS):
                            continue
                        if user is not None:
                            yield Turn("kimi-code", session, text.project_of(cwd), text.iso(ts),
                                       user, "\n".join(reply), [], path)
                        user, reply, ts = body, [], j.get("time")
                    elif kind == "context.append_message":
                        msg = j.get("message") or {}
                        if msg.get("role") != "assistant":
                            continue
                        for part in (msg.get("content") or []):
                            if isinstance(part, dict) and part.get("type") == "text":
                                reply.append(part.get("text", ""))
        except OSError:
            return
        if user is not None:
            yield Turn("kimi-code", session, text.project_of(cwd), text.iso(ts),
                       user, "\n".join(reply), [], path)
