#!/usr/bin/env python3
"""仓库提交守门器：多会话共用同一个工作区时，把"提交"这件事串行化、可审计。

背景（2026-09-16 实测踩到的）：这台机器上**同时有两个 agent 会话**在改
`agent-memory-layer`，两边共享同一个 `.git` 与工作目录。出过一次事故——
一边用 `git add -A` 提交，把另一边刚生成、还不想提交的开发探针脚本一起扫了进去。

所以本仓库约定：**提交与推送统一走这个脚本**，它做五件事：
  1. **加锁**：同一时刻只允许一个会话提交（锁在 `.git/repo_guard.lock`，陈旧锁 10 分钟回收）
  2. **对表**：`git fetch` 后检查本地是否落后/分叉（落后就先别提交，避免把别人的推送顶掉）
  3. **禁止 -A**：要求调用方显式给路径；且提交前**暂存区必须是空的**（有东西就说明别人已经暂存了
     或你之前 add 过，先 `git reset`）——这样绝不会把别人的改动顺手带上
  4. **守门**：跑 pytest / ruff / 泄漏扫描（可用 --skip-tests 跳过，但 CI 也会跑）
  5. **提交 + 推送**：commit 消息从文件读（避免 PowerShell 引号坑），最后打印前后 sha

用法：
    python tools/repo_guard.py --status                     # 看状态（不动任何东西）
    python tools/repo_guard.py --message-file msg.txt --paths src/aml/x.py tests/test_x.py
    python tools/repo_guard.py --message-file msg.txt --paths README.md --skip-tests --no-fetch
    python tools/repo_guard.py --handoff "改了什么" --paths src/aml/y.py   # 非主控会话：留交接单
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, ".git", "repo_guard.lock")
HANDOFF_DIR = os.path.join(ROOT, "state", "handoff")
STALE_LOCK_SEC = 600


def git(*args, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{(result.stderr or result.stdout).strip()[:300]}")
    return (result.stdout or "").strip()


def normalize(paths) -> list:
    """统一路径写法：全部用正斜杠（git 的口径），去掉 ./ 前缀，去重排序。

    注意：Windows 上只用 os.path.normpath 会把 / 变成 \\，反而和 git 的输出对不上
    （测试抓到的），所以 normpath 之后再统一成 /。
    """
    out = []
    for path in paths:
        cleaned = os.path.normpath(path.strip().replace("\\", "/")).replace("\\", "/")
        if cleaned and cleaned != "." and cleaned not in out:
            out.append(cleaned)
    return sorted(out)


def staged_paths() -> list:
    return normalize(git("diff", "--cached", "--name-only").splitlines())


def behind_or_diverged(fetch: bool = True) -> dict:
    """返回本地与 origin/main 的关系：behind / ahead / diverged。"""
    if fetch:
        git("fetch", "origin", check=False)
    local = git("rev-parse", "HEAD")
    try:
        remote = git("rev-parse", "origin/main")
    except RuntimeError:
        return {"local": local[:7], "remote": None, "state": "no-remote"}
    counts = git("rev-list", "--left-right", "--count", "origin/main...HEAD").split()
    behind, ahead = int(counts[0]), int(counts[1])
    state = "in-sync" if not behind and not ahead else ("behind" if behind and not ahead else
                                                        ("ahead" if ahead and not behind else "diverged"))
    return {"local": local[:7], "remote": remote[:7], "behind": behind, "ahead": ahead, "state": state}


# --------------------------------------------------------------------- 锁

def lock_acquire() -> bool:
    try:
        with open(LOCK, "x", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {dt.datetime.now().isoformat(timespec='seconds')}\n")
        return True
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(LOCK)
        except OSError:
            return True
        if age > STALE_LOCK_SEC:
            print(f"  [!] 发现陈旧锁（{age/60:.1f} 分钟前），接管")
            try:
                os.remove(LOCK)
            except OSError:
                return False
            return lock_acquire()
        print(f"  [x] 别人正在提交（锁 {LOCK} 已存在 {age/60:.1f} 分钟）——等一会儿再试")
        return False


def lock_release():
    try:
        os.remove(LOCK)
    except OSError:
        pass


# ----------------------------------------------------------------- 守门步骤

def run_checks(skip_tests: bool = False) -> bool:
    checks = []
    if not skip_tests:
        checks.append(([sys.executable, "-m", "pytest", "-q"], "pytest"))
    checks.append(([sys.executable, "-m", "ruff", "check", "src", "tests", "tools"], "ruff"))
    checks.append(([sys.executable, "tools/scrub_check.py"], "内容泄漏扫描"))
    for cmd, label in checks:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        tail = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
        print(f"  {'[ok]' if result.returncode == 0 else '[x] '} {label}"
              + ("" if result.returncode == 0 else f"：{tail[-1][:160] if tail else ''}"))
        if result.returncode != 0:
            return False
    return True


def status() -> int:
    rel = behind_or_diverged(fetch=False)
    print(f"分支状态：{rel['state']}（本地 {rel['local']}"
          + (f" / 远端 {rel['remote']}" if rel.get("remote") else "") + ")")
    staged = staged_paths()
    print(f"已暂存：{len(staged)} 个" + (f" —— {', '.join(staged[:8])}" if staged else "（空，符合约定）"))
    dirty = normalize(git("status", "--porcelain").splitlines())
    print(f"工作区改动：{len(dirty)} 项")
    for line in dirty[:20]:
        print(f"    {line}")
    if os.path.isdir(HANDOFF_DIR):
        notes = sorted(os.listdir(HANDOFF_DIR))[-3:]
        if notes:
            print(f"交接单（{len(os.listdir(HANDOFF_DIR))} 份，最近 3 份）：")
            for name in notes:
                print(f"    {name}")
    return 0


def handoff(note: str, paths: list) -> int:
    os.makedirs(HANDOFF_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(HANDOFF_DIR, f"{stamp}-pid{os.getpid()}.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# 交接单 {stamp}\n\n- 说明：{note}\n- 涉及文件：\n")
        for p in normalize(paths):
            f.write(f"  - `{p}`\n")
        f.write("\n（本文件在 state/ 下，不进仓库；主控会话提交前会读它）\n")
    print(f"已写交接单：{path}")
    return 0


# --------------------------------------------------------------------- 主流程

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="仓库提交守门器（多会话串行化 + 禁止 -A）")
    ap.add_argument("--status", action="store_true", help="只看状态")
    ap.add_argument("--handoff", help="非主控会话：写交接单（配合 --paths）")
    ap.add_argument("--message-file", help="commit 消息文件（UTF-8）")
    ap.add_argument("--paths", nargs="*", default=[], help="本次要提交的路径（必须显式列出）")
    ap.add_argument("--skip-tests", action="store_true", help="跳过 pytest（ruff 与泄漏扫描仍跑）")
    ap.add_argument("--no-fetch", action="store_true", help="不联网对表（离线/测试用）")
    ap.add_argument("--dry-run", action="store_true", help="一路检查但不提交")
    args = ap.parse_args(argv)

    if args.status:
        return status()
    if args.handoff:
        return handoff(args.handoff, args.paths)

    if not args.message_file or not args.paths:
        ap.error("提交需要 --message-file 与 --paths（禁止 git add -A，所以必须显式列路径）")
    if not os.path.isfile(args.message_file):
        print(f"找不到消息文件：{args.message_file}", file=sys.stderr)
        return 2
    message = open(args.message_file, encoding="utf-8").read().strip()
    if not message:
        print("消息文件是空的", file=sys.stderr)
        return 2

    print("== 提交守门 ==")
    if not lock_acquire():
        return 3
    try:
        rel = behind_or_diverged(fetch=not args.no_fetch)
        print(f"  对表：{rel['state']}（本地 {rel['local']}"
              + (f" / 远端 {rel.get('remote')}" if rel.get("remote") else "") + ")")
        if rel["state"] in ("behind", "diverged"):
            print("  [x] 远端有你没有的提交 —— 先 `git pull --rebase` 再提交（别强推）")
            return 4

        already = staged_paths()
        if already:
            print(f"  [x] 暂存区里已有 {len(already)} 个文件（{', '.join(already[:5])}）——"
                  f"可能是别人 add 的，先 `git reset` 清空再走本脚本")
            return 5

        wanted = normalize(args.paths)
        missing = [p for p in wanted if not os.path.exists(os.path.join(ROOT, p))
                   and not git("ls-files", "--error-unmatch", p, check=False)]
        if missing:
            print(f"  [x] 这些路径既不存在也没被跟踪：{', '.join(missing)}")
            return 6
        git("add", "--", *wanted)
        staged = staged_paths()
        extra = [p for p in staged if p not in wanted]
        if extra:
            print(f"  [x] 暂存区出现未列出的文件：{extra}（异常，已中止）")
            return 7
        print(f"  暂存：{len(staged)} 个文件 —— {', '.join(staged[:6])}"
              + (" …" if len(staged) > 6 else ""))

        if not run_checks(skip_tests=args.skip_tests):
            print("  [x] 守门未通过，已中止（改动仍在暂存区，修完再跑一次）")
            return 8

        if args.dry_run:
            print("  （--dry-run：不提交）")
            return 0
        git("commit", "-F", args.message_file)
        before = git("rev-parse", "HEAD")
        push = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if push.returncode != 0:
            print(f"  [x] push 失败：{(push.stderr or push.stdout).strip()[:200]}")
            return 9
        print(f"  [ok] 已推送：{before[:7]} → {git('rev-parse', 'HEAD')[:7]}")
        return 0
    finally:
        lock_release()


if __name__ == "__main__":
    raise SystemExit(main())
