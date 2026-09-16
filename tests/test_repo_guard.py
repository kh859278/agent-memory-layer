"""提交守门器的测试：路径规范化、陈旧锁、暂存区校验（都在临时 git 仓库里跑，不碰真仓库）。

守门器本身的价值就在"拦住手滑"，所以测试重点在**拒绝路径**：
  · 显式路径之外的东西进不了暂存区
  · 暂存区里已有别人的东西 → 直接中止
  · 陈旧锁能接管、新锁会挡住并发的第二个提交者
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import repo_guard  # noqa: E402


def test_normalize_paths_dedupes_and_unifies_separators():
    got = repo_guard.normalize(["./src/aml/x.py", "src\\aml\\x.py", "README.md", "  tests/a.py  "])
    assert got == ["README.md", "src/aml/x.py", "tests/a.py"]


def test_stale_lock_is_taken_over(tmp_path, monkeypatch):
    lock = tmp_path / "repo_guard.lock"
    monkeypatch.setattr(repo_guard, "LOCK", str(lock))
    assert repo_guard.lock_acquire() is True                 # 第一次拿到
    assert repo_guard.lock_acquire() is False                # 第二个提交者被挡
    repo_guard.lock_release()

    lock.write_text("99999 old\n", encoding="utf-8")
    old = time.time() - repo_guard.STALE_LOCK_SEC - 60
    os.utime(lock, (old, old))
    assert repo_guard.lock_acquire() is True                 # 陈旧锁可接管
    repo_guard.lock_release()


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "kept.txt").write_text("a\n", encoding="utf-8")
    (repo / "other.txt").write_text("b\n", encoding="utf-8")
    run("add", "--", "kept.txt", "other.txt")
    run("commit", "-q", "-m", "init")
    return repo


def test_guard_refuses_when_something_else_is_already_staged(tmp_path, monkeypatch):
    """别人已经 add 了文件时，守门器必须停下（这正是当初 add -A 事故的边界）。"""
    repo = make_repo(tmp_path)
    monkeypatch.setattr(repo_guard, "ROOT", str(repo))
    monkeypatch.setattr(repo_guard, "LOCK", str(repo / ".git" / "repo_guard.lock"))
    (repo / "kept.txt").write_text("changed\n", encoding="utf-8")
    (repo / "other.txt").write_text("changed too\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "other.txt"], cwd=repo, capture_output=True)

    msg = repo / "msg.txt"
    msg.write_text("test: 只该提交 kept.txt\n", encoding="utf-8")
    code = repo_guard.main(["--message-file", str(msg), "--paths", "kept.txt",
                            "--skip-tests", "--no-fetch"])
    assert code == 5                                          # 暂存区非空 → 拒绝
    staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo,
                            capture_output=True, text=True).stdout.split()
    assert staged == ["other.txt"]                            # 别人的暂存没被动过


def test_status_mode_is_read_only(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(repo_guard, "ROOT", str(repo))
    monkeypatch.setattr(repo_guard, "HANDOFF_DIR", str(repo / "state" / "handoff"))
    assert repo_guard.main(["--status"]) == 0
    assert subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo,
                          capture_output=True, text=True).stdout.strip() == ""


def test_handoff_writes_note_with_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_guard, "HANDOFF_DIR", str(tmp_path / "handoff"))
    assert repo_guard.main(["--handoff", "改了两处", "--paths", "src/a.py", "tests/b.py"]) == 0
    files = list((tmp_path / "handoff").iterdir())
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "改了两处" in text and "src/a.py" in text
