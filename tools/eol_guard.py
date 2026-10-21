#!/usr/bin/env python3
"""行尾守卫：jj 不读 .gitattributes / core.autocrlf，提交前把文本文件的 CRLF 归整成 LF。

背景（2026-10-21 实测）：本机 Git 的系统配置是 `core.autocrlf=true`（Git for Windows 默认），
`git checkout` 会把文本文件写成 CRLF；而 jj 的 snapshot 原样记录工作副本字节，不认识
`.gitattributes`，于是 CRLF 可能被提交进仓库，破坏本仓库“历史里统一 LF”的规范
（见 `.gitattributes`：`* text=auto eol=lf`）。这个工具是补偿层：按 git 的 `text` 属性
挑出文本文件，把 CRLF 改成 LF（`--fix`），检查模式只报告。

判定规则：
  · `git check-attr text` 为 unset → 跳过（二进制）
  · 前 8000 字节含 NUL → 跳过（二进制启发式）
  · 常见二进制扩展名 → 跳过（兜底）
  · 其余文本文件：出现 CRLF 即违规/修正（含 `.ps1` 这类 attr=eol=crlf 的文件：
    提交进历史的形态仍应是 LF，CRLF 只属于检出形态）

用法：
    python tools/eol_guard.py --paths a.md src/x.py          # 只检查
    python tools/eol_guard.py --paths a.md src/x.py --fix    # 自动改
    python tools/eol_guard.py --all-tracked --fix            # 全仓（CI 用）

退出码：0 干净或已修正；1 有违规（检查模式）；2 环境/git 错误。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".db", ".zip", ".gz", ".7z",
    ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".otf", ".mp3",
    ".mp4", ".wav", ".webp", ".bmp", ".pyc", ".whl", ".sqlite", ".sqlite3",
}
NUL_PROBE = 8000


def _git(args, cwd):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败：{(result.stderr or result.stdout).strip()[:300]}"
        )
    return result.stdout or ""


def repo_root(start=None):
    """当前目录所在的 git 仓库根目录。"""
    return _git(["rev-parse", "--show-toplevel"], start or os.getcwd()).strip()


def text_attrs(repo, paths):
    """批量取 git 的 text 属性：{path: set|auto|unset|unspecified}。"""
    attrs = {}
    chunk = []

    def flush():
        nonlocal chunk
        if not chunk:
            return
        raw = _git(["check-attr", "text", "--", *chunk], repo)
        for line in raw.splitlines():
            parts = line.rsplit(": ", 2)
            if len(parts) == 3:
                attrs[parts[0]] = parts[2].strip()
        chunk = []

    for path in paths:
        chunk.append(path)
        if len(chunk) >= 200:
            flush()
    flush()
    return attrs


def is_binary(full_path):
    """二进制启发式：NUL 或已知二进制扩展名。"""
    if os.path.splitext(full_path)[1].lower() in BINARY_EXTS:
        return True
    try:
        with open(full_path, "rb") as handle:
            head = handle.read(NUL_PROBE)
    except OSError:
        return True
    return b"\x00" in head


def check_paths(repo, paths, fix=False):
    """检查/修正一组仓库相对路径。

    返回 (fixed, violations, skipped)：
      · fixed      —— 已把 CRLF 改成 LF 的文件
      · violations —— 检查模式发现的 CRLF 文本文件
      · skipped    —— 二进制或 text=unset 而跳过的文件
    """
    fixed, violations, skipped = [], [], []
    existing = [p for p in paths if os.path.isfile(os.path.join(repo, p))]
    attrs = text_attrs(repo, existing)
    for path in paths:
        full = os.path.join(repo, path)
        if not os.path.isfile(full):
            continue  # 删除的文件不用查行尾
        if attrs.get(path) == "unset" or is_binary(full):
            skipped.append(path)
            continue
        with open(full, "rb") as handle:
            data = handle.read()
        if b"\r\n" not in data:
            continue
        if fix:
            with open(full, "wb") as handle:
                handle.write(data.replace(b"\r\n", b"\n"))
            fixed.append(path)
        else:
            violations.append(path)
    return fixed, violations, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="文本文件行尾守卫（CRLF→LF）：jj 不读 .gitattributes 的补偿"
    )
    parser.add_argument("--paths", nargs="*", default=[], help="要检查的路径（通常传本次变更文件）")
    parser.add_argument("--all-tracked", action="store_true", help="检查仓库所有已跟踪文件")
    parser.add_argument("--fix", action="store_true", help="把 CRLF 改成 LF")
    parser.add_argument("--repo", default=None, help="仓库根目录（默认取当前目录）")
    args = parser.parse_args(argv)

    repo = os.path.abspath(args.repo) if args.repo else repo_root()
    if args.all_tracked:
        paths = [p for p in _git(["ls-files", "-z"], repo).split("\0") if p]
    else:
        paths = [os.path.normpath(p) for p in args.paths]
    if not paths:
        print("没有要检查的路径（用 --paths 或 --all-tracked）")
        return 0

    fixed, violations, skipped = check_paths(repo, paths, fix=args.fix)
    if fixed:
        print(f"已归整 CRLF→LF：{len(fixed)} 个文件")
        for path in fixed[:20]:
            print(f"  [fix] {path}")
    if violations:
        print(f"发现 CRLF 文本文件：{len(violations)} 个（加 --fix 自动改）")
        for path in violations[:20]:
            print(f"  [x] {path}")
        return 1
    print(f"行尾检查通过：{len(paths) - len(skipped)} 个文本文件，{len(skipped)} 个二进制/属性跳过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"行尾守卫出错：{exc}", file=sys.stderr)
        raise SystemExit(2) from None
