"""适配器基类与共享类型。

新增一个 agent 只需要：继承 `Adapter`，实现 `discover()` 与 `turns()`。
每个 turn = 一条"用户任务" + 一条"agent 回复"，各自带原始时间戳。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .. import text


@dataclass
class Turn:
    agent: str
    session: str
    project: str
    ts: str                 # 原始时间（ISO），空串表示该文件没给
    user: str               # 用户说了什么
    reply: str              # agent 回复（可见文本部分）
    tools: list = field(default_factory=list)
    path: str = ""


class Adapter:
    """一个 agent 的会话格式适配器。"""

    name = "base"

    def __init__(self, options: dict | None = None, ingest: dict | None = None,
                 only_files=None, skip_files=None):
        self.options = options or {}
        self.ingest = ingest or {}
        self.only_files = {os.path.abspath(p) for p in (only_files or [])} or None
        self.skip_files = {os.path.abspath(p) for p in (skip_files or [])}

    # ---- 生命周期 ----
    @property
    def enabled(self) -> bool:
        return bool(self.options.get("enabled", True))

    def want(self, path) -> bool:
        p = os.path.abspath(str(path))
        if self.only_files is not None and p not in self.only_files:
            return False
        if p in self.skip_files:
            return False
        return not text.skip_path(p, self.ingest.get("skip_path_parts"))

    def discover(self) -> list:
        """列出候选会话文件（已按 want() 过滤）。"""
        raise NotImplementedError

    def turns(self, path):
        """解析一个会话文件，产出 Turn。"""
        raise NotImplementedError

    # ---- 便捷 ----
    def path_of(self, pattern_root: str, pattern: str) -> list:
        import glob
        return [p for p in glob.glob(str(Path(os.path.expanduser(pattern_root)) / pattern),
                                     recursive=True) if self.want(p)]

    def max_len(self) -> int:
        return int(self.ingest.get("max_len", 300))

    def min_len(self) -> int:
        return int(self.ingest.get("min_len", 8))
