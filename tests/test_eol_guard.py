"""行尾守卫的测试：CRLF 文本要能查到/改掉，二进制不能被动。"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import eol_guard  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    # 用 bytes 写，避免 Windows 文本模式把 \n 翻成 \r\n（这本身也是行尾问题的来源之一）
    (repo / ".gitattributes").write_bytes(b"* text=auto eol=lf\n*.ps1 text eol=crlf\n")
    (repo / "a.md").write_bytes(b"line1\nline2\n")
    (repo / "b.ps1").write_bytes(b"Write-Host 1\n")
    (repo / "bin.dat").write_bytes(b"\x00\x01\r\n")
    _git(repo, "add", "--", ".gitattributes", "a.md", "b.ps1", "bin.dat")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_check_mode_reports_crlf_text_and_skips_binary(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "a.md").write_bytes(b"line1\r\nline2\r\n")
    (repo / "b.ps1").write_bytes(b"Write-Host 1\r\n")
    fixed, violations, skipped = eol_guard.check_paths(
        str(repo), ["a.md", "b.ps1", "bin.dat"], fix=False)
    assert fixed == []
    assert sorted(violations) == ["a.md", "b.ps1"]
    assert "bin.dat" in skipped


def test_fix_normalizes_crlf_even_for_ps1(tmp_path):
    """*.ps1 的 eol=crlf 只影响检出；进历史的形态仍应是 LF（与 git 的入库归一化一致）。"""
    repo = make_repo(tmp_path)
    (repo / "a.md").write_bytes(b"line1\r\nline2\r\n")
    (repo / "b.ps1").write_bytes(b"Write-Host 1\r\n")
    fixed, violations, skipped = eol_guard.check_paths(
        str(repo), ["a.md", "b.ps1", "bin.dat"], fix=True)
    assert sorted(fixed) == ["a.md", "b.ps1"]
    assert violations == []
    assert (repo / "a.md").read_bytes() == b"line1\nline2\n"
    assert (repo / "b.ps1").read_bytes() == b"Write-Host 1\n"
    assert (repo / "bin.dat").read_bytes() == b"\x00\x01\r\n"  # 二进制一个字节都不动


def test_clean_text_files_are_noop(tmp_path):
    repo = make_repo(tmp_path)
    fixed, violations, skipped = eol_guard.check_paths(
        str(repo), ["a.md", "b.ps1", "missing.txt"], fix=True)
    assert fixed == [] and violations == []


def test_cli_all_tracked_runs(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    (repo / "a.md").write_bytes(b"x\r\n")
    monkeypatch.chdir(repo)
    assert eol_guard.main(["--all-tracked"]) == 1
    assert eol_guard.main(["--all-tracked", "--fix"]) == 0
    assert (repo / "a.md").read_bytes() == b"x\n"
