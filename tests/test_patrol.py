"""技能治理测试：指纹/比较/元数据/三条安全闸门/清单/播报。

不联网：上游目录用临时目录伪造，`packages` 的 registry 用 monkeypatch 替换。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml.patrol import packages, skills, update  # noqa: E402
from aml.patrol.notify import NoticeQueue  # noqa: E402


def make_skill(root, name, body="正文", extra=None, meta=None):
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: 测试技能 {name}\n---\n\n{body}\n")
    for rel, content in (extra or {}).items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    if meta:
        skills.write_meta(path, meta)
    return path


def make_cfg(tmp_path, roots):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["skill_roots"] = [{"name": f"root{i}", "path": str(root),
                                          "mirror_to": f"技能原始/root{i}"}
                                         for i, root in enumerate(roots)]
    cfg.data["patrol"]["snapshot_dirs"] = []
    return cfg


# ------------------------------------------------------------------ 基础

def test_dir_hash_ignores_line_endings_and_meta(tmp_path):
    a = make_skill(str(tmp_path / "a"), "s1", body="内容")
    b = make_skill(str(tmp_path / "b"), "s1", body="内容")
    # 把 b 的换行统一改成 CRLF，并塞一个元数据文件：指纹应当不变
    target = os.path.join(b, "SKILL.md")
    data = open(target, "rb").read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    open(target, "wb").write(data)
    skills.write_meta(b, {"name": "s1", "commit": "x"})
    assert skills.dir_hash(a) == skills.dir_hash(b)


def test_compare_dirs_reports_added_removed_changed(tmp_path):
    a = make_skill(str(tmp_path / "a"), "s1", body="旧内容", extra={"ref.md": "x"})
    b = make_skill(str(tmp_path / "b"), "s1", body="新内容", extra={"extra.md": "y"})
    cmp = skills.compare_dirs(a, b)
    assert cmp["only_a"] == ["ref.md"]
    assert cmp["only_b"] == ["extra.md"]
    assert cmp["differ"] == ["SKILL.md"]


def test_sync_dir_mirrors_and_bumps_mtime(tmp_path):
    src = make_skill(str(tmp_path / "src"), "s1", body="新")
    dst = make_skill(str(tmp_path / "dst"), "s1", body="旧", extra={"stale.md": "z"})
    old_mtime = os.path.getmtime(os.path.join(dst, "SKILL.md"))
    changed = skills.sync_dir(src, dst)
    assert "SKILL.md" in changed
    assert "-stale.md" in changed
    assert not os.path.exists(os.path.join(dst, "stale.md"))
    assert os.path.getmtime(os.path.join(dst, "SKILL.md")) >= old_mtime   # mtime 被刷新（增量灌库靠它）


def test_live_roots_skips_missing_paths(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["skill_roots"] = [
        {"name": "ok", "path": str(good), "mirror_to": "技能原始/ok"},
        {"name": "missing", "path": str(tmp_path / "nope"), "mirror_to": "技能原始/no"},
    ]
    roots = skills.live_roots(cfg)
    assert [r[0] for r in roots] == ["ok"]


# -------------------------------------------------------- 三条安全闸门

def test_gate_clean_local_gets_updated(tmp_path):
    """第三条闸门：本地干净 → 备份后覆盖。**前提是人批准过**（2026-09-22 收紧）。"""
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body="旧")
    up = make_skill(str(tmp_path / "up"), "s1", body="新")
    meta = {"name": "s1", "commit": "old", "content_hash": skills.dir_hash(live),
            "upstream_hash": "stalehash", "local_diff": False}
    # 没批准过 → 只暂存（拿不到自动更新权）
    action, why = update.update_one(cfg, "s1", live, meta, skills.meta_path(live), up, "newsha")
    assert action == "gated" and "只暂存待批" in why
    assert "旧" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()
    # 人批准这一版之后：**再来的上游新版**才会真的落地（上一轮已经暂存过了，
    # 同一版不会重复触发 —— 所以这里换一版上游内容）
    from aml.patrol import capability
    capability.record_approval(cfg, "s1", live, meta=meta, log=lambda *_: None)
    up2 = make_skill(str(tmp_path / "up2"), "s1", body="更新版")
    meta2 = {"name": "s1", "commit": "newsha", "content_hash": skills.dir_hash(live),
             "upstream_hash": skills.dir_hash(up), "local_diff": False}
    action, _ = update.update_one(cfg, "s1", live, meta2, skills.meta_path(live), up2, "sha2")
    assert action == "updated"
    assert "更新版" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()
    assert meta2["commit"] == "sha2" and meta2["local_diff"] is False


def test_gate_local_edit_is_only_staged(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body="我自己改过的")
    up = make_skill(str(tmp_path / "up"), "s1", body="上游新版")
    meta = {"name": "s1", "commit": "old", "content_hash": "指纹不匹配",
            "upstream_hash": "stale", "local_diff": False}
    action, _ = update.update_one(cfg, "s1", live, meta, skills.meta_path(live), up, "newsha")
    assert action == "staged"
    assert "我自己改过的" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()   # 本地没被动
    assert any(p.startswith("s1-") for p in os.listdir(cfg.state_dir / "patrol" / "_pending"))


def test_gate_local_patch_never_overwritten(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body="打了补丁的版本")
    up = make_skill(str(tmp_path / "up"), "s1", body="上游新版")
    meta = {"name": "s1", "commit": "old", "local_patch": "我改过路径",
            "content_hash": skills.dir_hash(live), "upstream_hash": "stale"}
    action, _ = update.update_one(cfg, "s1", live, meta, skills.meta_path(live), up, "newsha")
    assert action == "patched"
    assert "打了补丁的版本" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()


def test_accept_applies_staged_version(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body="旧的本地版")
    staged_src = make_skill(str(tmp_path / "up"), "s1", body="暂存的上游版")
    skills.write_meta(live, {"name": "s1", "repo": "o/r", "local_diff": True})
    skills.stage_skill(cfg, staged_src, "s1", "abc1234")
    result = update.accept(cfg, name="s1", log=lambda *_: None)
    assert result["accepted"] == ["s1"]
    assert "暂存的上游版" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()
    meta, _ = skills.read_meta(live)
    assert meta["local_diff"] is False
    assert skills.pending_items(cfg) == []          # 采纳后暂存目录被清掉


# ------------------------------------------------------------- 清单生成

def test_inventory_marks_tracked_patched_and_untracked(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "tracked", meta={"name": "tracked", "repo": "o/r", "subdir": "s/tracked"})
    make_skill(str(live), "patched", meta={"name": "patched", "repo": "o/r", "local_patch": "改过"})
    make_skill(str(live), "plain")
    cfg = make_cfg(tmp_path, [live])
    info = skills.write_inventory(cfg)
    text = open(info["inventory"], encoding="utf-8").read()

    assert info["total"] == 3 and info["tracked"] == 2 and info["patched"] == 1
    assert "已纳入上游跟踪（可自动更新）：**2**" in text
    assert "⚠ 有" in text
    assert "plain" in text and "未跟踪技能" in text


def test_mirror_dry_run_reports_without_writing(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "s1", body="内容")
    cfg = make_cfg(tmp_path, [live])
    report = skills.mirror(cfg, dry_run=True)
    assert report["root0"]["added_or_updated"] == 1
    assert not (cfg.knowledge_dir / "技能原始" / "root0").exists()     # 没写


def test_mirror_then_inventory_end_to_end(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "s1", body="内容")
    cfg = make_cfg(tmp_path, [live])
    report = skills.mirror(cfg)
    assert report["root0"]["added_or_updated"] == 1
    assert (cfg.knowledge_dir / "技能原始" / "root0" / "s1" / "SKILL.md").is_file()
    info = skills.write_inventory(cfg)
    assert info["total"] == 1


# ------------------------------------------------------------- 播报队列

def test_notice_brief_is_capped_and_empty_when_nothing_new(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["notify_max_chars"] = 100
    queue = NoticeQueue(cfg)
    assert queue.brief() == ""                       # 没内容就什么都不说
    queue.add("skills", "技能自动更新 2 个（tdd、grilling）")
    first = queue.brief()
    assert first.startswith("📌 ") and len(first) <= 100

    queue.add("packages", "DSH 有新版 " + "很长" * 80)
    combined = queue.brief()
    assert len(combined) <= 100                      # 多条也要压到上限内
    assert combined.endswith("…")


def test_notice_ack_moves_to_history(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    queue = NoticeQueue(cfg)
    first_id = queue.add("skills", "条目一")
    queue.add("skills", "条目二")
    assert len(queue.pending()) == 2
    assert queue.ack(notice_id=first_id) == 1
    assert len(queue.pending()) == 1
    assert queue.brief().endswith("条目二")
    assert queue.ack(all_=True) == 1
    assert queue.pending() == []
    assert queue.brief() == ""
    assert len(queue.data["history"]) == 2


# --------------------------------------------------------- 包版本监控

def test_semver_ordering_handles_prereleases():
    assert packages.semver_key("0.1.6-alpha.1") < packages.semver_key("0.1.6")
    assert packages.semver_key("0.1.5-rc.1") < packages.semver_key("0.1.5-rc.2")
    assert packages.semver_key("0.1.5-rc.2") < packages.semver_key("0.1.6-alpha.1")
    assert max(["0.1.0-rc.6", "0.1.1-rc.1", "0.1.5-rc.1"], key=packages.semver_key) == "0.1.5-rc.1"


def test_summarize_notes_extracts_bullets():
    body = ("# v1.2.0\n\n## What's Changed\n"
            "- add staged retrieval protocol by @someone\n"
            "- fix: windows encoding crash\n\n**Full Changelog**: ...\n")
    text = packages.summarize_notes(body, 70)
    assert "staged retrieval protocol" in text
    assert "Full Changelog" not in text and "#" not in text


def test_packages_check_notifies_only_when_newer(tmp_path, monkeypatch):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["packages"] = [{"name": "@demo/cli", "channel": "latest",
                                       "installed_glob": str(tmp_path / "**" / "package.json"),
                                       "releases_repo": "demo/cli"}]
    installed_dir = tmp_path / "_npx" / "abc" / "node_modules" / "@demo" / "cli"
    installed_dir.mkdir(parents=True)
    (installed_dir / "package.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")

    calls = {"n": 0}

    def fake_registry(name, base="", deadline=0):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"dist-tags": {"latest": "1.1.0"}, "versions": {"1.1.0": {}}, "time": {}}
        return {"dependencies": {"left-pad": "1.0.0"}}

    monkeypatch.setattr(packages, "registry", fake_registry)
    monkeypatch.setattr(packages.github, "release_notes",
                        lambda *a, **k: {"body": "- add shiny feature", "url": "u", "published": "2026-01-01"})
    result = packages.check(cfg, log=lambda *_: None)
    info = result["@demo/cli"]
    assert info["action"] == "notify" and info["target"] == "1.1.0" and info["current"] == "1.0.0"
    assert "shiny feature" in info["summary"]


def test_packages_check_silent_when_current(tmp_path, monkeypatch):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["packages"] = [{"name": "@demo/cli", "installed_glob":
                                       str(tmp_path / "**" / "package.json")}]
    installed_dir = tmp_path / "x" / "node_modules" / "@demo" / "cli"
    installed_dir.mkdir(parents=True)
    (installed_dir / "package.json").write_text(json.dumps({"version": "1.1.0"}), encoding="utf-8")
    monkeypatch.setattr(packages, "registry",
                        lambda name, base="", deadline=0: {"dist-tags": {"latest": "1.1.0"},
                                                           "versions": {}, "time": {}})
    result = packages.check(cfg, log=lambda *_: None)
    assert result["@demo/cli"]["action"] == "none"
