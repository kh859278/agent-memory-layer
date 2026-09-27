"""OpenCode 会话适配器。

与其它 agent 不同，OpenCode 把会话写进 **SQLite**（默认
`~/.local/share/opencode/opencode.db`），不是逐会话的 JSONL 文件 —— 所以
"扫文件"那套适配器看不见它。表结构（实测 1.18.32）：

  session(id, project_id, slug, title, directory, time_created, ...)
  message(id, session_id, time_created, data)   # data.role = user / assistant
  part(id, message_id, session_id, time_created, data)  # data.type = text / tool / ...

两个实现要点：

1. **必须连 WAL 一起读**：活跃库的正文都在 `-wal` 里，单开主库会看不到任何表。
   做法是把 db / -wal / -shm 一起拷到临时目录再读 —— 既看得到 WAL，
   又完全不碰用户的库（只读审计的既定姿势）。
2. 时间戳是**毫秒整数**，统一交给 `text.iso()`。

新增/删除会话不改变主库 mtime（WAL 才是写入路径），所以监控侧要盯
`opencode.db-wal`；这里 `discover()` 返回主库路径，由调用方决定何时读。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile

from .. import text
from .base import Adapter, Turn


def default_db() -> str:
    root = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(root, "opencode", "opencode.db")


class OpenCodeAdapter(Adapter):
    name = "opencode"

    def db_path(self) -> str:
        return os.path.expanduser(self.options.get("db_path") or default_db())

    def discover(self):
        if not self.enabled:
            return []
        db = self.db_path()
        if not os.path.exists(db):
            return []
        return [db] if self.want(db) else []

    # ---- 内部：把库（含 WAL）拷到临时目录再读 ----
    def _query(self, db):
        tmp = tempfile.mkdtemp(prefix="aml-opencode-")
        base = os.path.basename(db)
        try:
            for suffix in ("", "-wal", "-shm"):
                src = db + suffix
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(tmp, base + suffix))
            con = sqlite3.connect(os.path.join(tmp, base), timeout=15)
            try:
                sessions = con.execute(
                    "select id, directory, time_created from session").fetchall()
                messages = con.execute(
                    "select id, session_id, time_created, data from message"
                    " order by time_created").fetchall()
                parts = con.execute(
                    "select message_id, data from part order by time_created").fetchall()
            except sqlite3.Error:
                return [], [], []
            finally:
                con.close()
            return sessions, messages, parts
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _text_of(parts_of_message):
        out, tools = [], []
        for data in parts_of_message:
            try:
                p = json.loads(data) if isinstance(data, str) else data
            except ValueError:
                continue
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and p.get("text"):
                out.append(p["text"])
            elif p.get("type") == "tool" and p.get("tool"):
                tools.append(str(p["tool"]))
        return "\n".join(out).strip(), tools

    def turns(self, path):
        db = path or self.db_path()
        sessions, messages, parts = self._query(db)
        by_msg = {}
        for mid, data in parts:
            by_msg.setdefault(mid, []).append(data)
        sdir = {sid: (directory or "") for sid, directory, _ in sessions}
        # 按会话归组消息，顺序由 time_created 保证
        order = []
        grouped = {}
        for mid, sid, created, data in messages:
            try:
                m = json.loads(data) if isinstance(data, str) else data
            except ValueError:
                continue
            if not isinstance(m, dict):
                continue
            grouped.setdefault(sid, []).append((created, mid, m.get("role"), data))
            if sid not in order:
                order.append(sid)

        for sid in order:
            cwd = sdir.get(sid, "")
            user, reply, tools, ts = None, [], [], None
            for created, mid, role, _data in grouped[sid]:
                body, called = self._text_of(by_msg.get(mid, []))
                if role == "user":
                    if not body or body.startswith("<"):
                        continue
                    if user is not None:
                        yield Turn("opencode", sid, text.project_of(cwd), text.iso(ts),
                                   user, "\n".join(reply), tools, db)
                    user, reply, tools, ts = body, [], [], created
                elif role == "assistant":
                    if body:
                        reply.append(body)
                    tools += called
            if user is not None:
                yield Turn("opencode", sid, text.project_of(cwd), text.iso(ts),
                           user, "\n".join(reply), tools, db)
