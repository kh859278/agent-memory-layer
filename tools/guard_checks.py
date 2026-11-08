#!/usr/bin/env python3
"""守门器的子检查器包装：以子进程方式跑独立检查脚本（目前是体积门禁）。

为什么单独一个文件：repo_guard.py 自己也要过体积门禁（>20KB 拒绝提交），把这段
"调用检查脚本 + 转述输出"的胶水搬出来，repo_guard 才能保持在 20KB 以内；检查逻辑
仍各自留在 `tools/size_guard.py`（可单独运行、可单独挂 pre-commit）。
"""
from __future__ import annotations

import os
import subprocess
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
SIZE_GUARD = os.path.join(TOOLS_DIR, "size_guard.py")


def _spawn(script, args, repo_root, limit=24):
    """跑一个检查脚本；把输出缩进转述到终端，返回退出码。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, script, *args], cwd=repo_root, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", env=env)
    lines = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
    for line in lines[:limit]:
        print(f"  {line}")
    return result.returncode


def run_size_guard(paths, repo_root):
    """提交前体积门禁：新文件/增长到 >20KB(≈5000 token) 拒绝，存量超标只警告。"""
    if not os.path.isfile(SIZE_GUARD):
        print("  [x] 找不到 tools/size_guard.py（体积门禁缺失，拒绝提交）")
        return False
    code = _spawn(SIZE_GUARD, ["--repo", repo_root, "--paths", *paths], repo_root)
    if code != 0:
        print("  [x] 体积门禁未通过")
        return False
    print("  [ok] 体积门禁")
    return True


def print_size_audit(repo_root):
    """只读盘点：列全仓已跟踪的超标文件（存量，不拦提交）。"""
    if not os.path.isfile(SIZE_GUARD):
        print("体积门禁：找不到 tools/size_guard.py（未安装）")
        return
    print("体积门禁盘点（>20KB ≈ 5000 token；存量只警告）：")
    _spawn(SIZE_GUARD, ["--repo", repo_root, "--all-tracked"], repo_root, limit=14)
