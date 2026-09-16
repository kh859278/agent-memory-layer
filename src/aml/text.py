"""文本处理：截断、降噪判定、项目名归一、时间戳归一。

这些规则直接决定"什么内容进记忆层"，是从实战里长出来的，改动前先看注释。
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

# 界面回显 / 系统噪声 / 纯确认语：这些进了库只会挤占检索预算
UI_NOISE = re.compile(
    r"标准模式.*(对话|轨迹).*系统提示词|上下文注入|skill-catalog|Insufficient Balance"
    r"|API Error: \d+|^[\s\W_]+$")
CONFIRM_ONLY = re.compile(r"^(OK|ok|好的|收到|嗯|对|是的|可以|继续|开始|完成|谢谢)[。.!！]?$")


def clip(text: str, n: int = 300) -> str:
    """压平空白并截断——嵌入模型有效窗口有限，超长内容对检索是负担不是帮助。"""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text[:n] + ("…" if len(text) > n else "")


def keep(text: str, min_len: int = 8) -> bool:
    """这段文本值不值得入库。"""
    c = (text or "").strip()
    if len(c) < min_len:
        return False
    if UI_NOISE.search(c) or CONFIRM_ONLY.match(c):
        return False
    return True


def iso(value) -> str:
    """毫秒时间戳或 ISO 串统一成 ISO 串（Z 结尾）。"""
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        return value if value.endswith("Z") or "+" in value else value
    return ""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def project_of(cwd) -> str:
    """工作目录 → 项目名（标签用，避免特殊字符）。"""
    if not cwd:
        return "unknown"
    base = os.path.basename(str(cwd).rstrip("\\/")) or "root"
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", base)[:40]


def skip_path(path: str, parts) -> bool:
    """路径里含指定片段就跳过（默认挡 .dsh 内部目录与 node_modules）。"""
    p = str(path).replace("/", "\\").lower()
    return any(str(x).lower() in p for x in (parts or []))


def ensure_utf8_stdio() -> None:
    """把 stdout/stderr 强制成 UTF-8。

    为什么需要：中文 Windows 上 Python 默认按 GBK 编码 stdout，而 CLI 会打印中文与
    ✅/⚠️/❌ 这类字符，直接 `UnicodeEncodeError: 'gbk' codec can't encode character` 崩掉——
    而且崩在**全新安装后的第一条命令**（`aml doctor`）上，是最难看的位置。
    只在可执行入口调用，不在库导入时动全局状态。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 老版本 Python 或被重定向的流
            pass
