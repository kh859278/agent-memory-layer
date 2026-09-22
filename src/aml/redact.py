"""共用脱敏规则：**这一份正则只有这一个真相**。

同一套规则有两个消费者，过去它们各写各的（都在 `tools/scrub_check.py` 里）：

  · **提交前的内容泄漏扫描** —— CI 的 scrub job 与 `tools/repo_guard.py` 都跑它
  · **会话原文发给 LLM 之前** —— `distill.call_llm()`（2026-09-22 补上）

为什么必须合并成一份：两边规则一旦分叉，就会出现"扫描器认得的、发送路径不认得"的缝 ——
本机路径、密钥、邮箱在提交时被拦下，却在蒸馏时照发不误。这个缝正是外部评审
（2026-09-22）点出来的问题之一，所以规则表上移到 `src/`，`tools/scrub_check.py` 反过来 import 它。

本模块**刻意不依赖任何第三方库、也不 import 本包的其他模块**：
CI 的 scrub job 只做 checkout + setup-python，**没有 pip install**，靠 `tools/scrub_check.py`
把 `src/` 塞进 `sys.path` 才能 import 到这里（见该脚本的 `_ensure_src_on_path()`）。
只要本模块保持"只 import re"，那条路径就永远是稳的。
"""
from __future__ import annotations

import re

# 规则表：(标签, 正则)。改这里 = 同时改扫描与发送两条路径，这正是合并的目的。
PATTERNS = [
    ("绝对路径(Windows 用户目录)", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+")),
    # 注意：应用目录与占位账号名里也常出现 "home/xxx"（kimi-code/home/sessions、
    # /home/user/... 这类示例），所以用否定前瞻排除这些"不是真实用户家目录"的段。
    # 这条规则被自己的文档与测试判红过三次（2026-09-16），改动请连同自测一起跑。
    ("绝对路径(Unix 家目录)",
     re.compile(r"/(?:home|Users)/(?!(?:sessions|shared|runner|vscode|app|data|user|username|you|yourname)/)"
                r"[A-Za-z0-9._-]+/")),
    ("疑似 API key", re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|"
                                r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b")),
    ("疑似硬编码密钥赋值", re.compile(r"(?i)\b(?:api[_-]?key|secret|password|token)\s*[:=]\s*"
                                      r"[\"'][^\"'\s]{12,}[\"']")),
    ("邮箱", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
]

# 允许出现的"看起来像敏感信息"的写法：
#   · 文档里的占位符（<you>、example.com、OWNER/repo）
#   · GitHub 官方 noreply 邮箱与 SSH 用户：它们**设计上就是公开的**，
#     任何 GitHub 仓库里都会合理出现（提交作者、远程地址），不该算内容泄漏。
#     （2026-09-16 实测：加了这条之前，CI 会被自己的 ROADMAP 判红。）
ALLOW = re.compile(r"(?:Users[\\/]+<|/home/<|/Users/<|Users[\\/]+USER|example\.com|"
                   r"user@example|OWNER/agent-memory-layer|"
                   r"[\w.+-]*@users\.noreply\.github\.com|git@github\.com)")

# 脱敏后的占位符。用中文全角括号，避免和 Markdown/JSON 语法打架。
MASK = "［已脱敏］"


def _allowed_spans(text: str) -> list:
    return [m.span() for m in ALLOW.finditer(text)]


def _in_allowed(span: tuple, allowed: list) -> bool:
    """这个命中算不算"允许出现"。

    **判据是"有重叠"而不是"被完全包住"**（2026-09-22 修）：老实现是
    `ALLOW.sub("", line)` 之后再搜（先把允许片段抠掉），抠掉 `Users\\<` 之后
    `C:\\Users\\<you>` 就不再命中；换成"完全包住"会把这个占位符重新判成泄漏，
    实测直接把 CI 与 repo_guard 判红（4 处全是文档里的占位符）。
    所以这里用重叠判定，与老口径等价。
    """
    start, end = span
    return any(a < end and start < b for a, b in allowed)


def find_hits(text: str) -> list:
    """扫出一段文本里的疑似敏感信息：[(标签, 命中片段)]。

    只做检测、不改文本 —— `tools/scrub_check.py` 用它做提交前的守门。
    """
    allowed = _allowed_spans(text or "")
    hits = []
    for label, pattern in PATTERNS:
        for m in pattern.finditer(text or ""):
            if _in_allowed(m.span(), allowed):
                continue
            hits.append((label, m.group(0)[:80]))
    return hits


def redact(text: str, mask: str = MASK, extra_words=()) -> tuple:
    """把疑似敏感信息替换成占位符：返回 `(新文本, 命中标签去重列表)`。

    用途是**发送前**（`distill` 把会话原文交给 LLM 之前），所以取向与
    `find_hits` 不同：宁可多盖一点也不能漏 —— 发出去就收不回来了。

    `extra_words` 给"自定义屏蔽词"（客户名/项目名，来自 `tools/scrub_blocklist.txt`
    或配置），它们没有正则形态，按字面量整体替换。

    替换从右往左做：这样前面替换导致的位移不会影响后面还没处理的 span。
    """
    text = text or ""
    allowed = _allowed_spans(text)
    spans = []
    labels = []
    for label, pattern in PATTERNS:
        for m in pattern.finditer(text):
            if _in_allowed(m.span(), allowed):
                continue
            spans.append((m.start(), m.end(), label))
            labels.append(label)
    for word in extra_words or ():
        word = str(word or "")
        if not word:
            continue
        start = text.find(word)
        while start >= 0:
            spans.append((start, start + len(word), "屏蔽词"))
            labels.append("屏蔽词")
            start = text.find(word, start + len(word))
    # 重叠的 span 先合并，免得替换时把后一个 span 的坐标搞乱
    merged = []
    for start, end, label in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]), merged[-1][2])
        else:
            merged.append((start, end, label))
    out = text
    for start, end, _label in reversed(merged):
        out = out[:start] + mask + out[end:]
    seen, ordered = set(), []
    for label in labels:
        if label not in seen:
            seen.add(label)
            ordered.append(label)
    return out, ordered


__all__ = ["PATTERNS", "ALLOW", "MASK", "find_hits", "redact"]
