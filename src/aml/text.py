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

# 会话流水里的"样板文本"：harness 自己注入的系统提示、时间戳、子 agent 通知、日程提醒。
# 2026-11-20 实测：29,678 条有效记忆里有 7,632 条（25.7%）是这类文本；
# 用一条 "Time sampled…" 查询就能把 P1 的 5 个位子 / 1,500 字预算全部占满。
# 判据只认**开头**：正文中讨论这些字样（比如梳理采集规则）不该被误伤。
BOILERPLATE_PREFIXES = (
    "<system-reminder>",
    "Time sampled while preparing",
    "Current runtime context.",
    "This snapshot supersedes earlier runtime-context snapshots.",
    "[SCHEDULE REMINDER BATCH]",
    "Background subagent ",
    "Background job ",
    "<goal_round>",
    "<goal_complete>",
    "<goal_blocked>",
)
# 偶尔不以这些字样开头、但整段就是运行时快照的（同一句话出现在正文前 200 字内）
BOILERPLATE_MARKERS = (
    "This snapshot supersedes earlier runtime-context snapshots.",
)


def is_boilerplate(value: str) -> bool:
    """这段文本是不是 harness 自己产生的样板（不是用户/agent 的实际内容）。"""
    c = (value or "").lstrip()
    if not c:
        return True
    if c.startswith(BOILERPLATE_PREFIXES):
        return True
    return any(m in c[:200] for m in BOILERPLATE_MARKERS)


def clip(text: str, n: int = 300) -> str:
    """压平空白并截断——嵌入模型有效窗口有限，超长内容对检索是负担不是帮助。"""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text[:n] + ("…" if len(text) > n else "")


def keep(text: str, min_len: int = 8, *, allow_boilerplate: bool = False) -> bool:
    """这段文本值不值得入库。"""
    c = (text or "").strip()
    if len(c) < min_len:
        return False
    if UI_NOISE.search(c) or CONFIRM_ONLY.match(c):
        return False
    if not allow_boilerplate and is_boilerplate(c):
        return False
    return True


# "值得记"的信号词：决策 / 约束 / 结论 / 偏好 / 命令。
# 只在 `ingest.only_worthwhile: true` 时启用（默认关）——先剥样板、量效果，
# 再决定要不要把"流水账只留结论"变成默认，免得一次改太多没法归因。
_WORTH = re.compile(
    r"结论|决定|改成|改为|以后|约定|口径|规则|必须|禁止|不要|别再|注意|坑|踩过"
    r"|建议|优先|取舍|原因|实测|修复|根因|教训|记住|偏好")


def is_worthwhile(text: str, min_len: int = 20) -> bool:
    """这段内容像不像"可复用的经验"，而不只是过程流水。"""
    c = (text or "").strip()
    if len(c) < min_len:
        return False
    return bool(_WORTH.search(c))


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
    """工作目录 → 项目名（标签用，避免特殊字符）。

    必须同时按 `\\` 和 `/` 切：会话里记的 cwd 可能是 Windows 路径，
    而程序跑在 Linux/macOS 上（CI 就这样），只用 os.path.basename 会得到
    `C-work-proj-a` 这种整串（2026-09-16 CI 抓到）。
    """
    if not cwd:
        return "unknown"
    parts = [p for p in re.split(r"[\\/]+", str(cwd).rstrip("\\/")) if p]
    base = parts[-1] if parts else "root"
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", base)[:40]


def domain_of(value) -> str:
    """领域名归一：**一个人面文件必须能装下它**。

    为什么需要（2026-09-24 实测）：库里出现了 12 条以 `<ktype>/<domain>` 形态写进来的
    domain（`checklist/env-setup`、`pitfall/data-pipeline`、`tooling/web-deploy`…）。
    带斜杠的 domain 映射不成 `沉淀/<domain>.md` 这个文件名，
    于是这些条目**只在机器面存在、人面永远看不到** —— 两副面孔当场破功。

    规则：分隔符（`/`、`\\`、空白）一律折成 `-`，连续 `-` 合并，去掉首尾 `-`；
    空值回落到 `general`。**不做语义改写**（不猜"checklist 其实是 ktype"），
    保持可逆 —— 语义层的一次性修正交给迁移脚本，不藏在写入路径里。
    """
    s = re.sub(r"[/\\\s]+", "-", str(value or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "general"


def skip_path(path: str, parts) -> bool:
    """路径里含指定片段就跳过（默认挡 .dsh 内部目录与 node_modules）。"""
    p = str(path).replace("/", "\\").lower()
    return any(str(x).lower() in p for x in (parts or []))


def write_lf(path, content: str) -> None:
    """写文本文件，强制 LF 换行、UTF-8。

    为什么不用 `Path.write_text(..., newline="\\n")`：**`newline` 参数是 Python 3.10 才加的**，
    在 3.9 上会 `TypeError`（CI 的 3.9 矩阵就是这么红的，2026-09-16 复现）。
    """
    directory = os.path.dirname(str(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


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
