"""提交守门器的测试：路径规范化、陈旧锁、暂存区校验（都在临时 git 仓库里跑，不碰真仓库）。

守门器本身的价值就在"拦住手滑"，所以测试重点在**拒绝路径**：
  · 显式路径之外的东西进不了暂存区
  · 暂存区里已有别人的东西 → 直接中止
  · 陈旧锁能接管、新锁会挡住并发的第二个提交者
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pytest

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


# ---------------------------------------------------------------- jj 模式

REAL_EOL_GUARD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "eol_guard.py")
JJ_FALLBACK = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           "Programs", "jj-0.46.0", "jj.exe")


def _jj():
    return shutil.which("jj") or (JJ_FALLBACK if os.path.isfile(JJ_FALLBACK) else None)


def make_jj_repo(tmp_path):
    """临时 git 仓库 + jj colocated（不联网、无 origin）。"""
    repo = tmp_path / "jjrepo"
    repo.mkdir()

    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "kept.txt").write_text("a\n", encoding="utf-8")
    run("add", "--", "kept.txt")
    run("commit", "-qm", "init")
    subprocess.run([_jj(), "git", "init"], cwd=repo, capture_output=True, text=True)
    return repo


def patch_guard_for(monkeypatch, repo):
    monkeypatch.setattr(repo_guard, "ROOT", str(repo))
    monkeypatch.setattr(repo_guard, "LOCK", str(repo / ".git" / "repo_guard.lock"))
    monkeypatch.setattr(repo_guard, "HANDOFF_DIR", str(repo / "state" / "handoff"))
    monkeypatch.setattr(repo_guard, "EOL_GUARD", REAL_EOL_GUARD)
    monkeypatch.setattr(repo_guard, "run_checks", lambda **kwargs: True)


@pytest.mark.skipif(_jj() is None, reason="需要本机安装 jj")
def test_jj_guard_commits_and_normalizes_eol_without_push(tmp_path, monkeypatch):
    """jj 模式：显式路径通过、CRLF 归整成 LF、自动移动 main 书签、不推送。"""
    repo = make_jj_repo(tmp_path)
    patch_guard_for(monkeypatch, repo)
    (repo / "kept.txt").write_bytes(b"changed\r\nsecond\r\n")
    msg = tmp_path / "msg.txt"          # 消息文件必须在仓库外：jj 会整副本提交
    msg.write_text("test: jj commit\n", encoding="utf-8")
    code = repo_guard.main(["--message-file", str(msg), "--paths", "kept.txt",
                            "--skip-tests", "--no-fetch", "--no-push"])
    assert code == 0
    desc = subprocess.run([_jj(), "log", "-r", "main", "--no-graph", "-T", "description"],
                          cwd=repo, capture_output=True, text=True).stdout
    assert "test: jj commit" in desc
    blob = subprocess.run(["git", "cat-file", "-p", "main:kept.txt"], cwd=repo,
                          capture_output=True).stdout
    assert blob == b"changed\nsecond\n"   # CRLF 已被行尾守卫归整


@pytest.mark.skipif(_jj() is None, reason="需要本机安装 jj")
def test_jj_guard_rejects_unlisted_change(tmp_path, monkeypatch):
    repo = make_jj_repo(tmp_path)
    patch_guard_for(monkeypatch, repo)
    (repo / "kept.txt").write_text("changed\n", encoding="utf-8")
    (repo / "other.txt").write_text("untracked\n", encoding="utf-8")
    msg = tmp_path / "msg.txt"          # 消息文件必须在仓库外
    msg.write_text("test: 应被拒绝\n", encoding="utf-8")
    code = repo_guard.main(["--message-file", str(msg), "--paths", "kept.txt",
                            "--skip-tests", "--no-fetch", "--no-push"])
    assert code == 7                       # 有未列出的改动 → 拒绝（对应 add -A 事故边界）
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert "应被拒绝" not in log           # 没有产生提交


def test_git_mode_no_push_commits_locally(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    patch_guard_for(monkeypatch, repo)
    (repo / "kept.txt").write_text("changed\n", encoding="utf-8")
    msg = repo / "msg.txt"
    msg.write_text("test: git commit no push\n", encoding="utf-8")
    code = repo_guard.main(["--message-file", str(msg), "--paths", "kept.txt",
                            "--skip-tests", "--no-fetch", "--no-push"])
    assert code == 0
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=repo,
                         capture_output=True, text=True).stdout
    assert "test: git commit no push" in log
