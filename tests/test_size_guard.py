"""体积门禁的测试：新建/增长到 >20KB 要拦，存量超标只警告，锁文件与产物跳过。

口径来自 tidy skill：单文件超过 5,000 token（≈ 20 KB，按字节/4 估算）拒绝提交。
本机仓库是 colocated jj，jj 不跑 git hooks，所以门禁挂在守门器里；这里同时测
`size_guard` 的判定与 `repo_guard` 的接线（git 模式）。
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import repo_guard  # noqa: E402
import size_guard  # noqa: E402


def _run(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "t@example.com")
    _run(repo, "config", "user.name", "t")
    (repo / "small.txt").write_bytes(b"hello\n")
    _run(repo, "add", "--", "small.txt")
    _run(repo, "commit", "-qm", "init")
    return repo


def write_big(path, size=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (size if size is not None else size_guard.LIMIT_BYTES + 1024))
    return path


def test_estimate_tokens_is_bytes_over_four():
    assert size_guard.estimate_tokens(4_000) == 1_000
    assert size_guard.estimate_tokens(size_guard.LIMIT_BYTES) == 5_120


def test_small_file_passes(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "small.txt").write_bytes(b"hello world\n")
    result = size_guard.check_paths(str(repo), ["small.txt"])
    assert result["blocked"] == [] and result["warned"] == []
    assert result["checked"] == 1


def test_exact_limit_boundary_passes(tmp_path):
    """正好等于上限不拦（>，不是 >=）。"""
    repo = make_repo(tmp_path)
    write_big(repo / "boundary.txt", size_guard.LIMIT_BYTES)
    result = size_guard.check_paths(str(repo), ["boundary.txt"])
    assert result["blocked"] == []


def test_new_oversized_file_is_blocked(tmp_path):
    repo = make_repo(tmp_path)
    write_big(repo / "huge.py")
    result = size_guard.check_paths(str(repo), ["huge.py"])
    assert [p for p, _ in result["blocked"]] == ["huge.py"]


def test_existing_oversized_file_only_warns(tmp_path):
    """存量超标：基线（main）上已经超了 → 只警告，不卡旧文件的正常小改。"""
    repo = make_repo(tmp_path)
    write_big(repo / "legacy.py")
    _run(repo, "add", "--", "legacy.py")
    _run(repo, "commit", "-qm", "legacy big file")
    (repo / "legacy.py").write_bytes(b"y" * (size_guard.LIMIT_BYTES + 2048))
    result = size_guard.check_paths(str(repo), ["legacy.py"])
    assert result["blocked"] == []
    assert [p for p, _ in result["warned"]] == ["legacy.py"]


def test_file_growing_from_small_to_oversized_is_blocked(tmp_path):
    """原来 ≤ 上限、这次长成 > 上限 → 拦（防止悄悄变巨无霸）。"""
    repo = make_repo(tmp_path)
    (repo / "small.txt").write_bytes(b"z" * (size_guard.LIMIT_BYTES + 10))
    result = size_guard.check_paths(str(repo), ["small.txt"])
    assert [p for p, _ in result["blocked"]] == ["small.txt"]


def test_lockfile_build_output_and_minified_are_skipped(tmp_path):
    repo = make_repo(tmp_path)
    write_big(repo / "package-lock.json")
    write_big(repo / "dist" / "app.js")
    write_big(repo / "app.min.js")
    result = size_guard.check_paths(
        str(repo), ["package-lock.json", "dist/app.js", "app.min.js"])
    assert result["blocked"] == [] and result["warned"] == []
    reasons = dict(result["skipped"])
    assert reasons["package-lock.json"] == "锁文件"
    assert reasons["dist/app.js"] == "目录 dist/"
    assert "app.min.js" in reasons


def test_binary_and_generated_files_are_skipped(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "blob.exe").parent.mkdir(parents=True, exist_ok=True)
    (repo / "blob.exe").write_bytes(b"\x00\x01" * 20_000)
    (repo / "gen.py").write_bytes(b"# @generated\n" + b"x" * (size_guard.LIMIT_BYTES + 1024))
    result = size_guard.check_paths(str(repo), ["blob.exe", "gen.py"])
    assert result["blocked"] == [] and result["warned"] == []
    reasons = dict(result["skipped"])
    assert reasons["blob.exe"] == "二进制"
    assert reasons["gen.py"] == "自动生成"


def test_all_tracked_audit_lists_oversized_only(tmp_path):
    repo = make_repo(tmp_path)
    write_big(repo / "legacy.py")
    _run(repo, "add", "--", "legacy.py")
    _run(repo, "commit", "-qm", "legacy big file")
    found = size_guard.oversized_tracked(str(repo))
    assert [p for p, _ in found] == ["legacy.py"]


def test_cli_paths_exit_codes(tmp_path, capsys):
    repo = make_repo(tmp_path)
    write_big(repo / "huge.py")
    assert size_guard.main(["--repo", str(repo), "--paths", "small.txt"]) == 0
    assert size_guard.main(["--repo", str(repo), "--paths", "huge.py"]) == 1
    out = capsys.readouterr().out
    assert "体积门禁未通过" in out


def test_cli_all_tracked_is_read_only(tmp_path, capsys):
    repo = make_repo(tmp_path)
    write_big(repo / "legacy.py")
    _run(repo, "add", "--", "legacy.py")
    _run(repo, "commit", "-qm", "legacy big file")
    assert size_guard.main(["--repo", str(repo), "--all-tracked"]) == 0
    assert "只读盘点" in capsys.readouterr().out


# ---------------------------------------------------------------- 守门器接线

def patch_guard(monkeypatch, repo):
    monkeypatch.setattr(repo_guard, "ROOT", str(repo))
    monkeypatch.setattr(repo_guard, "LOCK", str(repo / ".git" / "repo_guard.lock"))
    monkeypatch.setattr(repo_guard, "HANDOFF_DIR", str(repo / "state" / "handoff"))
    monkeypatch.setattr(repo_guard, "run_checks", lambda **kwargs: True)


def test_repo_guard_blocks_oversized_new_file(tmp_path, monkeypatch):
    """接线：git 模式提交新增超重文件 → 退出码 14，且没有产生提交。"""
    repo = make_repo(tmp_path)
    patch_guard(monkeypatch, repo)
    (repo / "huge.py").write_bytes(b"print(1)\n" * 5_000)
    msg = tmp_path / "msg.txt"
    msg.write_text("test: 应被体积门禁拦下\n", encoding="utf-8")
    code = repo_guard.main(["--message-file", str(msg), "--paths", "huge.py",
                            "--skip-tests", "--no-fetch", "--no-push"])
    assert code == 14
    log = _run(repo, "log", "--oneline").stdout
    assert "应被体积门禁拦下" not in log


def test_repo_guard_allows_small_file(tmp_path, monkeypatch):
    """接线：小文件照常提交（门禁不误伤）。"""
    repo = make_repo(tmp_path)
    patch_guard(monkeypatch, repo)
    (repo / "small.txt").write_text("changed\n", encoding="utf-8")
    msg = tmp_path / "msg.txt"
    msg.write_text("test: 小改动\n", encoding="utf-8")
    code = repo_guard.main(["--message-file", str(msg), "--paths", "small.txt",
                            "--skip-tests", "--no-fetch", "--no-push"])
    assert code == 0
    assert "test: 小改动" in _run(repo, "log", "--oneline").stdout


def test_status_prints_size_audit(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    patch_guard(monkeypatch, repo)
    assert repo_guard.main(["--status"]) == 0
    assert "体积门禁" in capsys.readouterr().out
