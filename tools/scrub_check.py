#!/usr/bin/env python3
"""内容泄漏扫描：提交前挡住"不该进公开仓库的东西"。

为什么需要它：这套程序是从一台**真实在用的机器**上抽出来的，源码里很容易残留
本机绝对路径、用户名、客户/项目名、API key。靠人眼 review 一定会漏，所以放进 CI 当守门。

扫描项：
  1. 绝对路径：`C:\\Users\\<某人>\\...`、`/home/<某人>/...`、`/Users/<某人>/...`
  2. 疑似凭据：sk- / ghp_ / AKIA / xoxb- / 明显的 api_key = "..." 赋值
  3. 邮箱、手机号
  4. 自定义屏蔽词：`tools/scrub_blocklist.txt`（每行一个，`#` 注释）—— 客户名/项目名放这里，
     **不要**提交真实的屏蔽词文件内容（该文件默认只有注释）

用法：
    python tools/scrub_check.py            # 扫全仓库（跳过 .git / 二进制）
    python tools/scrub_check.py --staged   # 只扫 git 暂存区内容
退出码：0 = 干净，1 = 有命中（CI 会 fail）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKLIST = os.path.join(ROOT, "tools", "scrub_blocklist.txt")

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv",
             "build", "dist", "node_modules", ".mypy_cache"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
            ".db", ".sqlite", ".pyc", ".woff", ".woff2", ".ttf"}

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


def load_blocklist() -> list:
    if not os.path.isfile(BLOCKLIST):
        return []
    words = []
    with open(BLOCKLIST, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.append(line)
    return words


def target_files(staged: bool, all_files: bool = False) -> list:
    """默认只扫"会被提交的文件"（git 跟踪的 + 未忽略的新文件），
    所以本地配置文件（gitignore 掉的 config.local.yaml）不会误报。"""
    if staged:
        try:
            out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                                 capture_output=True, text=True, cwd=ROOT, check=True).stdout
            return [os.path.join(ROOT, p.strip()) for p in out.splitlines() if p.strip()]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    if not all_files:
        try:
            out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"],
                                 capture_output=True, text=True, cwd=ROOT, check=True).stdout
            files = [os.path.join(ROOT, p.strip()) for p in out.splitlines() if p.strip()]
            return [f for f in files if os.path.isfile(f)
                    and os.path.splitext(f)[1].lower() not in SKIP_EXT]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    files = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if os.path.splitext(name)[1].lower() in SKIP_EXT:
                continue
            files.append(os.path.join(base, name))
    return files


def scan_file(path: str, blocklist: list) -> list:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    hits = []
    rel = os.path.relpath(path, ROOT)
    for lineno, line in enumerate(lines, 1):
        if ALLOW.search(line):
            line_checked = ALLOW.sub("", line)
        else:
            line_checked = line
        for label, pattern in PATTERNS:
            m = pattern.search(line_checked)
            if m:
                hits.append((rel, lineno, label, m.group(0)[:80]))
        for word in blocklist:
            if word and word in line:
                hits.append((rel, lineno, "屏蔽词", word))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true", help="只扫 git 暂存区")
    ap.add_argument("--all", action="store_true", help="连被 .gitignore 忽略的文件一起扫")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    blocklist = load_blocklist()
    files = target_files(args.staged, all_files=args.all)
    hits = []
    for path in files:
        hits += scan_file(path, blocklist)

    if hits:
        print(f"❌ 内容泄漏扫描：{len(hits)} 处命中（{len(files)} 个文件）")
        for rel, lineno, label, sample in hits[:80]:
            print(f"  {rel}:{lineno}  [{label}]  {sample}")
        if len(hits) > 80:
            print(f"  …还有 {len(hits) - 80} 处")
        print("\n处置：改成占位符（如 C:\\Users\\<you>\\...）或用配置/环境变量传入；"
              "客户名/项目名加进 tools/scrub_blocklist.txt（该文件不进仓库）。")
        return 1
    if not args.quiet:
        print(f"✅ 内容泄漏扫描通过（{len(files)} 个文件，屏蔽词 {len(blocklist)} 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
