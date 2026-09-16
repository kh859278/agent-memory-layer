"""回归测试：CI 跨平台矩阵抓出来的两个 bug（本地单平台永远看不见）。

两个 bug 都在 2026-09-16 由 GitHub Actions 抓到，本地（Windows + 3.12）全绿：
  1. `project_of()` 只用 os.path.basename：Linux/macOS 上拿到 `C:\\work\\proj-a` 这样的
     Windows 路径时不会切开，项目名变成 `C-work-proj-a`。
  2. 通知 id 精确到毫秒：Windows 时钟粒度约 15.6ms，同一 tick 内连加两条 id 相同，
     `ack --id` 会把两条一起标记掉（测试里表现为 ack 返回 2 而不是 1）。
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import config as cfgmod  # noqa: E402
from aml.patrol.notify import NoticeQueue  # noqa: E402
from aml.text import project_of  # noqa: E402


def test_project_of_handles_both_separators_on_any_os():
    assert project_of(r"C:\work\proj-a") == "proj-a"
    assert project_of("C:/work/proj-a") == "proj-a"
    assert project_of("/home/user/proj-b") == "proj-b"
    assert project_of(r"\\server\share\proj-c") == "proj-c"
    assert project_of("proj") == "proj"
    assert project_of("") == "unknown"
    # 中文与空格也要能变成合法标签
    assert project_of(r"D:\工作\我的 项目") == "我的-项目"


def test_notice_ids_are_unique_within_same_clock_tick(tmp_path):
    """Windows 的时钟粒度粗，连加两条必须拿到不同 id。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    queue = NoticeQueue(cfg)
    ids = [queue.add("skills", f"第 {i} 条更新") for i in range(5)]
    assert len(set(ids)) == 5

    # 只 ack 一条，就应该只标记一条（历史里也只有一条）
    assert queue.ack(notice_id=ids[0]) == 1
    assert len(queue.data["history"]) == 1
    assert queue.data["history"][0]["id"] == ids[0]
    assert len(queue.pending()) == 4

    # 其余仍然会被播报，且 brief 不重复
    brief = queue.brief()
    assert brief.startswith("📌 ")
    assert "第 0 条更新" not in brief


def test_brief_is_empty_when_nothing_pending(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    queue = NoticeQueue(cfg)
    assert queue.brief() == ""
    queue.add("skills", "有更新")
    assert queue.brief() != ""
    queue.ack(all_=True)
    assert queue.brief() == ""
