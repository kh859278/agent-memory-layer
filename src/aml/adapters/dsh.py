"""DeepSeek Harness（DSH）会话适配器。

会话文件是 zstd 压缩的 JSONL：`~/.dsh/sessions/<session-id>/session*.jsonl.zstd`。
需要 `zstandard`（可选依赖）：没装就跳过该适配器并给出提示，而不是崩掉。
"""
from __future__ import annotations

import glob
import io
import json
import os

from .. import text
from .base import Adapter, Turn

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None


class DshAdapter(Adapter):
    name = "dsh"

    def discover(self):
        if zstd is None or not self.enabled:
            return []
        root = os.path.expanduser(self.options.get("sessions_dir") or "~/.dsh/sessions")
        return [p for p in glob.glob(os.path.join(root, "*", "*", "session*.jsonl.zstd"))
                if self.want(p)]

    def turns(self, path):
        session = os.path.basename(os.path.dirname(path)).replace("session-", "")[:36]
        dec = zstd.ZstdDecompressor()
        user, reply, tools, ts, cwd = None, [], [], None, None
        try:
            with open(path, "rb") as f, dec.stream_reader(f) as reader:
                for line in io.TextIOWrapper(reader, encoding="utf-8", errors="replace"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    kind = j.get("type")
                    if kind == "session":
                        cwd = j.get("cwd") or cwd
                        ts = j.get("createdAt")
                    elif kind == "user/message":
                        data = j.get("data") or {}
                        body = "\n".join(p.get("text", "") for p in (data.get("content") or [])
                                         if isinstance(p, dict) and p.get("type") == "text").strip()
                        if not body:
                            continue
                        if user is not None:
                            yield Turn("dsh", session, text.project_of(cwd), text.iso(ts),
                                       user, "\n".join(reply), tools, path)
                        user, reply, tools, ts = body, [], [], j.get("time")
                    elif kind == "assistant/message":
                        msg = ((j.get("data") or {}).get("message") or {})
                        for part in (msg.get("content") or []):
                            if not isinstance(part, dict):
                                continue
                            if part.get("type") == "text":
                                reply.append(part.get("text", ""))
                            elif part.get("type") == "tool-call":
                                tools.append(part.get("name", "?"))
        except Exception:  # noqa: BLE001 - 单个会话坏了不该拖垮整轮采集
            return
        if user is not None:
            yield Turn("dsh", session, text.project_of(cwd), text.iso(ts),
                       user, "\n".join(reply), tools, path)

    def archived_sessions(self):
        """读 DSH 的 workspace 注册表，返回已归档会话 id 集合（归档=用户明确"这段结束了"）。"""
        path = os.path.expanduser(self.options.get("archive_registry") or "~/.dsh/storages/workspace.json")
        if not os.path.isfile(path):
            return set()
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return set()
        ids = ((data.get("global") or {}).get("archivedSessionIds")) or []
        return set(ids)
