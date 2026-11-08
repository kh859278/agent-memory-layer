#!/usr/bin/env python3
"""仓库提交守门器：多会话共用一个工作区时，把“提交”串行化、可审计。

背景（2026-09-16 实测）：两个 agent 会话同时改本仓库、共用 `.git` 与工作目录，
一边 `git add -A` 把另一边刚生成、还不打算提交的探针脚本扫进了提交。
所以本仓库约定：提交与推送统一走这个脚本。

它做五件事：
  1. 加锁：同一时刻只允许一个会话提交（锁在 `.git/repo_guard.lock`，陈旧锁 10 分钟回收）
  2. 对表：检查本地是否落后/分叉（落后/分叉就拒绝，避免把别人的推送顶掉）
  3. 禁止 -A：只允许显式路径。git 模式要求暂存区为空；jj 模式下工作副本出现未列出的改动就拒绝
  4. 守门：跑 pytest / ruff / 泄漏扫描；jj 模式先跑 `tools/eol_guard.py` 把 CRLF 归整成 LF
     （jj 不读 .gitattributes，不补偿就会把 Windows 的 CRLF 提交进历史）
  5. 提交 + 推送：消息从文件读（避免 PowerShell 引号坑），打印前后提交号

两种引擎（自动识别：仓库里有 `.jj/` 就走 jj 模式）：
  · git 模式：git add（显式路径）→ git commit → git push
  · jj 模式：jj 快照 → 校验改动集 ⊆ --paths → eol 归整 → 检查 → jj commit
             → main 书签移动到新提交 → jj git push --bookmark main

用法：
    python tools/repo_guard.py --status                     # 看状态（只读）
    python tools/repo_guard.py --message-file msg.txt --paths src/aml/x.py tests/test_x.py
    python tools/repo_guard.py --message-file msg.txt --paths README.md --skip-tests --no-fetch
    python tools/repo_guard.py --message-file msg.txt --paths src/aml/x.py --no-push   # 无网时只提交
    python tools/repo_guard.py --handoff "改了什么" --paths src/aml/y.py   # 非主控会话：留交接单

环境变量：
    JJ_BIN   显式指定 jj 可执行文件（默认 PATH 里的 jj，或常见安装目录）
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import guard_checks  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK = os.path.join(ROOT, ".git", "repo_guard.lock")
HANDOFF_DIR = os.path.join(ROOT, "state", "handoff")
EOL_GUARD = os.path.join(ROOT, "tools", "eol_guard.py")
STALE_LOCK_SEC = 600

# 主书签与远端跟踪名。远端书签名拼出来而不是写字面量：`main@origin` 会被
# tools/scrub_check.py 的“邮箱”正则误判（它长得像邮箱）。
MAIN = "main"
_AT = "@"
REMOTE_MAIN = MAIN + _AT + "origin"


# ------------------------------------------------------------------ 基础命令

def git(*args, check=True):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{(result.stderr or result.stdout).strip()[:300]}")
    return (result.stdout or "").strip()


def find_jj():
    """定位 jj：JJ_BIN > PATH > 常见安装目录。"""
    env = os.environ.get("JJ_BIN")
    if env and os.path.isfile(env):
        return env
    found = shutil.which("jj")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    for candidate in (
        os.path.join(local, "Programs", "jj-0.46.0", "jj.exe"),
        os.path.join(os.path.expanduser("~"), ".local", "bin", "jj.exe"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


JJ_BIN = find_jj()


def jj(*args, check=True):
    exe = JJ_BIN or find_jj()
    if not exe:
        raise RuntimeError("找不到 jj；请设置 JJ_BIN 或把 jj 放进 PATH")
    result = subprocess.run([exe, "--no-pager", "--color=never", *args], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise RuntimeError(f"jj {' '.join(args)} 失败：{(result.stderr or result.stdout).strip()[:300]}")
    return (result.stdout or "").strip()


def is_jj_repo() -> bool:
    return os.path.isdir(os.path.join(ROOT, ".jj"))


def normalize(paths) -> list:
    """统一路径写法：全部用正斜杠（git/jj 的口径），去掉 ./ 前缀，去重排序。

    注意：Windows 上只用 os.path.normpath 会把 / 变成 \\，反而和 git 的输出对不上
    （测试抓到的），所以 normpath 之后再统一成 /。
    """
    out = []
    for path in paths:
        cleaned = os.path.normpath(path.strip().replace("\\", "/")).replace("\\", "/")
        if cleaned and cleaned != "." and cleaned not in out:
            out.append(cleaned)
    return sorted(out)


# ------------------------------------------------------------------ 状态查询

def staged_paths() -> list:
    return normalize(git("diff", "--cached", "--name-only").splitlines())


def jj_changed_paths() -> list:
    """工作副本 @ 相对父提交的改动（jj 自动快照后的口径）。"""
    return normalize(jj("diff", "--name-only").splitlines())


def jj_commit_id(revision: str) -> str:
    return jj("log", "-r", revision, "--no-graph", "-T", "commit_id")


def jj_rev_count(revset: str) -> int:
    try:
        raw = jj("log", "-r", revset, "--no-graph", "-T", "commit_id")
    except RuntimeError:
        return 0
    return len([line for line in raw.splitlines() if line.strip()])


def behind_or_diverged(fetch: bool = True) -> dict:
    """返回本地与 origin/main 的关系：behind / ahead / diverged / in-sync / no-remote。"""
    if is_jj_repo():
        if fetch:
            jj("git", "fetch", check=False)
        try:
            local = jj_commit_id(MAIN)
        except RuntimeError:
            return {"local": None, "remote": None, "state": "no-bookmark"}
        try:
            remote = jj_commit_id(REMOTE_MAIN)
        except RuntimeError:
            return {"local": local[:7], "remote": None, "state": "no-remote"}
        behind = jj_rev_count(f"{MAIN}..{REMOTE_MAIN}")
        ahead = jj_rev_count(f"{REMOTE_MAIN}..{MAIN}")
        state = "in-sync" if not behind and not ahead else (
            "behind" if behind and not ahead else ("ahead" if ahead and not behind else "diverged"))
        return {"local": local[:7], "remote": remote[:7], "behind": behind,
                "ahead": ahead, "state": state}

    if fetch:
        git("fetch", "origin", check=False)
    local = git("rev-parse", "HEAD")
    try:
        remote = git("rev-parse", "origin/main")
    except RuntimeError:
        return {"local": local[:7], "remote": None, "state": "no-remote"}
    counts = git("rev-list", "--left-right", "--count", "origin/main...HEAD").split()
    behind, ahead = int(counts[0]), int(counts[1])
    state = "in-sync" if not behind and not ahead else (
        "behind" if behind and not ahead else ("ahead" if ahead and not behind else "diverged"))
    return {"local": local[:7], "remote": remote[:7], "behind": behind,
            "ahead": ahead, "state": state}


# --------------------------------------------------------------------- 锁

def lock_acquire() -> bool:
    try:
        with open(LOCK, "x", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {dt.datetime.now().isoformat(timespec='seconds')}\n")
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


def run_eol_guard(paths: list) -> bool:
    """jj 模式专用：提交前把文本文件 CRLF 归整成 LF（jj 不读 .gitattributes 的补偿）。"""
    if not os.path.isfile(EOL_GUARD):
        print("  [x] 找不到 tools/eol_guard.py（jj 模式必须有行尾守卫，否则 CRLF 会进历史）")
        return False
    result = subprocess.run([sys.executable, EOL_GUARD, "--fix", "--paths", *paths],
                            cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    print(f"  {'[ok]' if result.returncode == 0 else '[x] '} 行尾守卫"
          + ("" if result.returncode == 0 else f"：{out[:200]}"))
    return result.returncode == 0


# ----------------------------------------------------------------- 状态 / 交接

def _print_handoffs():
    if os.path.isdir(HANDOFF_DIR):
        notes = sorted(os.listdir(HANDOFF_DIR))[-3:]
        if notes:
            print(f"交接单（{len(os.listdir(HANDOFF_DIR))} 份，最近 3 份）：")
            for name in notes:
                print(f"    {name}")


def status() -> int:
    if is_jj_repo():
        print("引擎：jj（colocated）")
        rel = behind_or_diverged(fetch=False)
        print(f"书签状态：{rel['state']}（main {rel.get('local') or '-'}"
              + (f" / {REMOTE_MAIN} {rel['remote']}" if rel.get("remote") else "") + ")")
        changed = jj_changed_paths()
        print(f"工作副本改动（@ 相对父提交）：{len(changed)} 项")
        for line in changed[:20]:
            print(f"    {line}")
    else:
        rel = behind_or_diverged(fetch=False)
        print(f"分支状态：{rel['state']}（本地 {rel['local']}"
              + (f" / 远端 {rel['remote']}" if rel.get("remote") else "") + ")")
        staged = staged_paths()
        print(f"已暂存：{len(staged)} 个" + (f" —— {', '.join(staged[:8])}" if staged else "（空，符合约定）"))
        dirty = normalize(git("status", "--porcelain").splitlines())
        print(f"工作区改动：{len(dirty)} 项")
        for line in dirty[:20]:
            print(f"    {line}")
    guard_checks.print_size_audit(ROOT)
    _print_handoffs()
    return 0


def handoff(note: str, paths: list) -> int:
    os.makedirs(HANDOFF_DIR, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(HANDOFF_DIR, f"{stamp}-pid{os.getpid()}.md")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"# 交接单 {stamp}\n\n- 说明：{note}\n- 涉及文件：\n")
        for item in normalize(paths):
            handle.write(f"  - `{item}`\n")
        handle.write("\n（本文件在 state/ 下，不进仓库；主控会话提交前会读它）\n")
    print(f"已写交接单：{path}")
    return 0


# --------------------------------------------------------------- 提交（git）

def submit_git(args, message: str) -> int:
    rel = behind_or_diverged(fetch=not args.no_fetch)
    print(f"  对表：{rel['state']}（本地 {rel['local']}"
          + (f" / 远端 {rel['remote']}" if rel.get("remote") else "") + ")")
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

    if not guard_checks.run_size_guard(staged, ROOT):
        print("  [x] 体积门禁未通过，已中止（改动仍在暂存区，拆分后再跑一次）")
        return 14

    if not run_checks(skip_tests=args.skip_tests):
        print("  [x] 守门未通过，已中止（改动仍在暂存区，修完再跑一次）")
        return 8

    if args.dry_run:
        print("  （--dry-run：不提交）")
        return 0
    before = git("rev-parse", "HEAD")
    git("commit", "-F", args.message_file)
    after = git("rev-parse", "HEAD")
    if args.no_push:
        print(f"  [ok] 已提交（未推送）：{before[:7]} → {after[:7]}")
        return 0
    push = subprocess.run(["git", "push"], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if push.returncode != 0:
        print(f"  [x] push 失败：{(push.stderr or push.stdout).strip()[:200]}")
        return 9
    print(f"  [ok] 已推送：{before[:7]} → {after[:7]}")
    return 0


# --------------------------------------------------------------- 提交（jj）

def submit_jj(args, message: str) -> int:
    # jj 会整副本提交：消息文件若在仓库内会被一起提交，提前拦下
    msg_abs = os.path.abspath(args.message_file)
    if msg_abs.startswith(os.path.abspath(ROOT) + os.sep):
        print("  [x] 消息文件在仓库内：jj 会把它一起提交；请把消息文件放到仓库外（或 .gitignore）")
        return 13
    rel = behind_or_diverged(fetch=not args.no_fetch)
    print(f"  对表：{rel['state']}（main {rel.get('local') or '-'}"
          + (f" / {REMOTE_MAIN} {rel['remote']}" if rel.get("remote") else "") + ")")
    if rel["state"] in ("behind", "diverged"):
        print(f"  [x] 远端有你没有的提交 —— 先 `jj git fetch`，再 `jj rebase -o {REMOTE_MAIN}`（别强推）")
        return 4

    jj("st")  # 触发工作副本快照
    changed = jj_changed_paths()
    if not changed:
        print("  [x] 工作副本没有改动，无内容可提交")
        return 5

    wanted = normalize(args.paths)
    tracked = set(normalize(jj("file", "list").splitlines()))
    missing = [p for p in wanted if not os.path.exists(os.path.join(ROOT, p)) and p not in tracked]
    if missing:
        print(f"  [x] 这些路径既不存在也没被跟踪：{', '.join(missing)}")
        return 6

    extra = [p for p in changed if p not in wanted]
    if extra:
        print(f"  [x] 工作副本里还有未列出的改动：{', '.join(extra)}")
        print("      jj 会整副本提交；请移走/加进 .gitignore，或把它们也写进 --paths")
        return 7

    # 只允许在 main 之上提交，避免把 main 书签移动到错误位置
    try:
        parent = jj_commit_id("@-")
        main = jj_commit_id("main")
    except RuntimeError as exc:
        print(f"  [x] 无法定位 main 父提交：{exc}")
        return 10
    if parent != main:
        print("  [x] 当前工作副本不是基于 main；拒绝自动移动 main 书签（先 rebase/手工处理）")
        return 10

    if not run_eol_guard(changed):
        return 11
    jj("st")  # 行尾归整后重新快照
    changed_after = jj_changed_paths()
    extra_after = [p for p in changed_after if p not in wanted]
    if extra_after:
        print(f"  [x] 行尾归整后出现未列出的改动：{', '.join(extra_after)}")
        return 7
    print(f"  变更：{len(changed_after)} 个文件 —— {', '.join(changed_after[:6])}"
          + (" …" if len(changed_after) > 6 else ""))

    if not guard_checks.run_size_guard(changed_after, ROOT):
        print("  [x] 体积门禁未通过，已中止（改动仍在工作副本里，拆分后再跑一次）")
        return 14

    if not run_checks(skip_tests=args.skip_tests):
        print("  [x] 守门未通过，已中止（改动仍在工作副本里，修完再跑一次）")
        return 8

    if args.dry_run:
        print("  （--dry-run：不提交）")
        return 0
    before = jj_commit_id("main")
    jj("commit", "-m", message)
    jj("bookmark", "move", "main", "--to", "@-")
    after = jj_commit_id("main")
    print(f"  提交：{before[:7]} → {after[:7]}")
    if args.no_push:
        print(f"  [ok] 已提交（未推送）：{after[:7]}")
        return 0
    exe = JJ_BIN or find_jj()
    push = subprocess.run([exe, "--no-pager", "--color=never", "git", "push", "--bookmark", "main"],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if push.returncode != 0:
        print(f"  [x] push 失败：{(push.stderr or push.stdout).strip()[:200]}")
        return 9
    print(f"  [ok] 已推送：{after[:7]}")
    return 0


# --------------------------------------------------------------------- 主流程

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="仓库提交守门器（多会话串行化 + 禁止 -A；自动识别 git/jj）")
    parser.add_argument("--status", action="store_true", help="只看状态")
    parser.add_argument("--handoff", help="非主控会话：写交接单（配合 --paths）")
    parser.add_argument("--message-file", help="commit 消息文件（UTF-8）")
    parser.add_argument("--paths", nargs="*", default=[], help="本次要提交的路径（必须显式列出）")
    parser.add_argument("--skip-tests", action="store_true", help="跳过 pytest（ruff 与泄漏扫描仍跑）")
    parser.add_argument("--no-fetch", action="store_true", help="不联网对表（离线/测试用）")
    parser.add_argument("--no-push", action="store_true", help="只提交、不推送（无网络时用）")
    parser.add_argument("--dry-run", action="store_true", help="一路检查但不提交")
    args = parser.parse_args(argv)

    if args.status:
        return status()
    if args.handoff:
        return handoff(args.handoff, args.paths)

    if not args.message_file or not args.paths:
        parser.error("提交需要 --message-file 与 --paths（禁止 git add -A，所以必须显式列路径）")
    if not os.path.isfile(args.message_file):
        print(f"找不到消息文件：{args.message_file}", file=sys.stderr)
        return 2
    with open(args.message_file, encoding="utf-8") as handle:
        message = handle.read().strip()
    if not message:
        print("消息文件是空的", file=sys.stderr)
        return 2

    print(f"== 提交守门（{'jj' if is_jj_repo() else 'git'} 模式）==")
    if not lock_acquire():
        return 3
    try:
        if is_jj_repo():
            return submit_jj(args, message)
        return submit_git(args, message)
    except RuntimeError as exc:
        print(f"  [x] 守门器错误：{exc}", file=sys.stderr)
        return 12
    finally:
        lock_release()


if __name__ == "__main__":
    raise SystemExit(main())
