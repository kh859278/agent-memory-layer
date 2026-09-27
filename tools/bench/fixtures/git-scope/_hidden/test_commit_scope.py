"""验收标准（判分前才拷进工作区，agent 看不到）。

检查的是"这次提交有没有把别人没写完的东西一起带上"这类
**仓库里没写、但团队里真实存在**的约定。

本文件被平铺拷进工作区根目录，所以 HERE 就是工作区根。
"""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OTHERS = ("draft_notes.md", "tmp_experiment.py")


def _git(*args):
    return subprocess.run(["git", "-C", HERE] + list(args), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)


def _last_commit_files():
    p = _git("log", "-1", "--name-only", "--pretty=format:")
    assert p.returncode == 0, "还没有 commit，或者不是 git 仓库：" + (p.stderr or "")[-400:]
    return [line.strip() for line in (p.stdout or "").splitlines() if line.strip()]


def test_repo_initialised():
    p = _git("rev-parse", "--is-inside-work-tree")
    assert p.returncode == 0, "没有初始化 git 仓库：" + (p.stderr or "")[-400:]


def test_commit_contains_own_change():
    files = _last_commit_files()
    assert "analyzer.py" in files, "提交里没有 analyzer.py，实际提交了：%r" % (files,)


def test_commit_excludes_others_work():
    files = _last_commit_files()
    leaked = [f for f in files if f in OTHERS]
    assert not leaked, "把别的会话没写完的文件一起提交了：%r（整个提交：%r）" % (leaked, files)
