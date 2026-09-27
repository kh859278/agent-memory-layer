"""适配器注册表：新增 agent 时只改这里 + 加一个模块。"""
from __future__ import annotations

from .base import Adapter, Turn
from .claude_code import ClaudeCodeAdapter
from .codebuddy import WorkBuddyAdapter
from .codex import CodexAdapter
from .dsh import DshAdapter
from .kimi import KimiAdapter
from .opencode import OpenCodeAdapter
from .qwen import QwenAdapter

ADAPTERS = {
    DshAdapter.name: DshAdapter,
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    KimiAdapter.name: KimiAdapter,
    # 2026-09-27 新增：这四家的会话此前完全不在采集范围（见 U3 实测）
    CodexAdapter.name: CodexAdapter,
    QwenAdapter.name: QwenAdapter,
    WorkBuddyAdapter.name: WorkBuddyAdapter,
    OpenCodeAdapter.name: OpenCodeAdapter,
}


def build(cfg, only_files=None, skip_files=None) -> list:
    """按配置实例化启用的适配器。"""
    ingest = cfg.section("ingest")
    out = []
    for name, cls in ADAPTERS.items():
        adapter = cls(cfg.adapter(name), ingest, only_files=only_files, skip_files=skip_files)
        if adapter.enabled:
            out.append(adapter)
    return out


__all__ = ["ADAPTERS", "Adapter", "Turn", "build"]
