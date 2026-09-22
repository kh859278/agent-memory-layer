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
  5. **二进制数据库直接判红**（2026-09-22 加，见下）

规则表本身在 `src/aml/redact.py`（**唯一真相**）：同一份正则也用在"蒸馏前给会话脱敏"那条
发送路径上。以前两边各写一份，缝隙就是"扫描器认得的、发送路径不认得"。

用法：
    python tools/scrub_check.py            # 扫全仓库（git 跟踪的 + 未忽略的新文件）
    python tools/scrub_check.py --staged   # 只扫 git 暂存区内容
    python tools/scrub_check.py --all      # 连被 .gitignore 忽略的文件一起扫
退出码：0 = 干净，1 = 有命中（CI 会 fail）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOCKLIST = os.path.join(ROOT, "tools", "scrub_blocklist.txt")
# 本地副本（gitignore 掉的）：真实客户名/项目名写这里，仓库里只留模板。
# 文档一直是这么写的，但代码直到 2026-09-22 才真的读它 —— 文档里的能力必须真的存在。
BLOCKLIST_LOCAL = os.path.join(ROOT, "tools", "scrub_blocklist.local.txt")


def _ensure_src_on_path() -> None:
    """让本脚本能 import `aml.redact`。

    为什么需要：CI 的 scrub job 只有 checkout + setup-python，**没有 pip install**，
    所以不能假设 `aml` 已装好。把 `src/` 插进 sys.path，这条守门就不依赖安装步骤
    （`redact.py` 只 import re，所以这条路走得通）。
    """
    src = os.path.join(ROOT, "src")
    if src not in sys.path:
        sys.path.insert(0, src)


_ensure_src_on_path()

from aml.redact import find_hits  # noqa: E402  （必须在 sys.path 处理之后）


def _fix_stdout_encoding() -> None:
    """把 stdout 钉成 UTF-8。

    2026-09-22 实测踩到的：中文 Windows 控制台默认 GBK，脚本最后那句 `✅ ...` 直接抛
    `UnicodeEncodeError`，于是**扫描本身没报错、最后一行把人送走**——repo_guard 拿到非零
    返回码就把提交拒了，人却以为"有泄漏"（fail-closed 是安全的，但这是个排障陷阱）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # 3.7+
        except (AttributeError, ValueError):  # pragma: no cover - 老解释器/被重定向
            pass


_fix_stdout_encoding()

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv",
             "build", "dist", "node_modules", ".mypy_cache"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
            ".pyc", ".woff", ".woff2", ".ttf"}
# 数据库文件**不再"跳过"而是直接判红**（2026-09-22 改）：原来它们在 SKIP_EXT 里，
# 等于给"整库记忆/整张会话表落进仓库"开了个静默通道——而正则扫二进制也没意义。
# 判红比跳过诚实：要么把数据搬出仓库，要么明说这次要放行（改动本脚本 = 留痕）。
DB_EXT = {".db", ".sqlite", ".sqlite3", ".db3"}


def load_blocklist() -> list:
    """词表 = 仓库里的模板 + 本地副本（`.local.txt`，被 gitignore）。两份都读。"""
    words = []
    for path in (BLOCKLIST, BLOCKLIST_LOCAL):
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and line not in words:
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
    """扫一个文件：[(相对路径, 行号, 标签, 片段)]。规则来自 `aml.redact`。"""
    ext = os.path.splitext(path)[1].lower()
    rel = os.path.relpath(path, ROOT)
    if ext in DB_EXT:
        return [(rel, 0, "二进制数据库", f"{os.path.getsize(path)} 字节 —— 不该进仓库")]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    hits = []
    for lineno, line in enumerate(lines, 1):
        for label, sample in find_hits(line):
            hits.append((rel, lineno, label, sample))
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
            where = f"{rel}:{lineno}" if lineno else rel
            print(f"  {where}  [{label}]  {sample}")
        if len(hits) > 80:
            print(f"  …还有 {len(hits) - 80} 处")
        print("\n处置：改成占位符（如 C:\\Users\\<you>\\...）或用配置/环境变量传入；"
              "客户名/项目名加进 tools/scrub_blocklist.txt（该文件不进仓库）；"
              "数据库文件搬出仓库（它里面有整库记忆，正则救不了）。")
        return 1
    if not args.quiet:
        scope = "含被忽略文件" if args.all else "git 跟踪 + 未忽略的新文件"
        print(f"✅ 内容泄漏扫描通过（{len(files)} 个文件，范围：{scope}，"
              f"屏蔽词 {len(blocklist)} 个）")
        if not blocklist:
            print("   ⚠ 屏蔽词 0 个：客户名/项目名这一层目前**没有防护**，"
                  "请把词写进 tools/scrub_blocklist.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
