"""锁文件与状态总览的测试（离线、只碰临时目录）。

要钉死的口径：
  · `local_diff` = 本地指纹 ≠ 元数据里记的指纹（或显式标了 local_patch）—— 上游更新时只暂存
  · 同名技能装在多个目录/作用域时，锁文件里合并 targets 并标出多作用域
  · 未纳管的技能也要出现在状态里（不能"看不见就当不存在"）
  · 锁文件默认：全局 → 知识库根；项目作用域 → 项目根（别的工具靠它发现项目）
  · 锁文件**只有元数据与哈希，没有技能正文**
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml.patrol import capability, lockfile, skills  # noqa: E402


def make_skill(root, name, body="正文"):
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: 测试技能 {name}\n---\n\n{body}\n")
    return path


def make_cfg(tmp_path, roots=None, scopes_cfg=None, **patrol):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["skill_roots"] = roots if roots is not None else []
    if scopes_cfg is not None:
        cfg.data["patrol"]["scopes"] = scopes_cfg
    cfg.data["patrol"].update(patrol)
    return cfg


def live_root(cfg) -> str:
    """测试用：拿到配置里第一个存在的技能目录（作用域化之后不能再直接读 skill_roots）。"""
    from aml.patrol import scopes
    for root in scopes.all_roots(cfg):
        if root["exists"]:
            return root["path"]
    raise AssertionError("测试夹具里没有活目录")


def tracked(cfg, name, repo="owner/repo", subdir="skills/x", commit="abcdef1234", dirty=False,
            extra=None):
    """在 global 作用域里造一个"已纳管"的技能。"""
    root = live_root(cfg)
    path = make_skill(str(root), name, body="改过的" if dirty else "原始")
    meta = {"name": name, "repo": repo, "subdir": subdir, "branch": "main", "commit": commit,
            "content_hash": "OLDHASH" if dirty else skills.dir_hash(path),
            "upstream_hash": skills.dir_hash(path), "local_diff": bool(dirty),
            "installed_at": "2026-09-18", "last_checked": "2026-09-18"}
    meta.update(extra or {})
    skills.write_meta(path, meta)
    return path


def base_cfg(tmp_path, scopes_cfg=None):
    live = tmp_path / "live"
    live.mkdir(exist_ok=True)
    return make_cfg(tmp_path, scopes_cfg=scopes_cfg or [
        {"name": "global", "kind": "global", "roots": [
            {"name": "live", "path": str(live), "sync_kb": True, "mirror_to": "技能原始/live"}]}])


# ------------------------------------------------------------------ 锁文件

def test_lock_builds_entries_with_hashes_and_targets(tmp_path):
    cfg = base_cfg(tmp_path)
    tracked(cfg, "tdd")
    data = lockfile.build(cfg)
    entry = data["skills"]["tdd"]
    assert data["lockfileVersion"] == 1 and data["generator"].startswith("agent-memory-layer/")
    assert entry["repo"] == "owner/repo" and entry["subdir"] == "skills/x"
    assert entry["commit"] == "abcdef1234" and entry["scope"] == "global"
    assert entry["targets"] == ["live"] and entry["synced_to_kb"] is True
    assert entry["local_diff"] is False
    assert data["scopes"]["global"]["sync_kb"] == ["live"]


def test_lock_marks_dirty_skills(tmp_path):
    cfg = base_cfg(tmp_path)
    tracked(cfg, "dirty-one", dirty=True)
    tracked(cfg, "patched-one", extra={"local_patch": "我改过路径"})
    data = lockfile.build(cfg)
    assert data["skills"]["dirty-one"]["local_diff"] is True
    assert data["skills"]["patched-one"]["local_patch"] is True


def test_lock_includes_untracked_skills(tmp_path):
    """未纳管的技能也要在锁文件里：`scope` 说的是"住在哪个作用域"（未纳管也住在那儿），
    "有没有上游"看 `repo` 是不是 None —— 两件事别混成一个字段。"""
    cfg = base_cfg(tmp_path)
    root = live_root(cfg)
    make_skill(str(root), "plain")
    entry = lockfile.build(cfg)["skills"]["plain"]
    assert entry["repo"] is None and entry["scope"] == "global"
    assert entry["targets"] == ["live"] and entry["commit"] is None


def test_lock_never_contains_skill_body(tmp_path):
    cfg = base_cfg(tmp_path)
    root = live_root(cfg)
    make_skill(str(root), "secretive", body="这是技能正文不该出现在锁文件里")
    path = lockfile.write(cfg, log=lambda *_: None)
    raw = open(path, encoding="utf-8").read()
    assert "不该出现在锁文件里" not in raw
    assert json.loads(raw)["skills"]["secretive"]["content_hash"]


def test_lock_merges_targets_when_skill_lives_in_two_dirs(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    cfg = make_cfg(tmp_path, scopes_cfg=[{"name": "global", "kind": "global", "roots": [
        {"name": "a", "path": str(a)}, {"name": "b", "path": str(b)}]}])
    for root in (a, b):
        make_skill(str(root), "shared")
    entry = lockfile.build(cfg)["skills"]["shared"]
    assert entry["targets"] == ["a", "b"]


def test_lock_default_path_global_vs_project(tmp_path):
    cfg = base_cfg(tmp_path)
    assert lockfile.default_path(cfg).parent == cfg.knowledge_dir
    project = tmp_path / "proj"
    project.mkdir()
    cfg2 = make_cfg(tmp_path, scopes_cfg=[{"name": "project:proj", "kind": "project",
                                           "path": str(project),
                                           "roots": [{"name": "p", "path": "skills"}]}])
    assert lockfile.default_path(cfg2, scope={"kind": "project", "path": str(project)}) \
        == project / "skills-lock.json"


def test_lock_write_and_render(tmp_path):
    cfg = base_cfg(tmp_path)
    tracked(cfg, "tdd")
    text = lockfile.render_lock(cfg)
    assert "锁文件" in text and "tdd" in text and "owner/repo" in text
    out = tmp_path / "custom" / "skills-lock.json"
    path = lockfile.write(cfg, path=str(out), log=lambda *_: None)
    assert os.path.isfile(path) and json.loads(open(path, encoding="utf-8").read())["skills"]


# ------------------------------------------------------------------ 状态总览

def test_status_rows_flag_dirty_staged_untracked(tmp_path):
    cfg = base_cfg(tmp_path)
    tracked(cfg, "clean")
    tracked(cfg, "dirty", dirty=True)
    root = live_root(cfg)
    make_skill(str(root), "plain")
    skills.stage_skill(cfg, os.path.join(str(root), "clean"), "clean", "abc1234")
    rows = {r["name"]: r for r in lockfile.status_rows(cfg)}
    assert rows["clean"]["staged"] is True and rows["clean"]["tracked"] is True
    assert rows["dirty"]["local_diff"] is True
    assert rows["plain"]["tracked"] is False and rows["plain"]["repo"] == "(未纳管)"
    assert rows["clean"]["state"] in capability.STATES


def test_status_render_summarizes_counts(tmp_path):
    cfg = base_cfg(tmp_path)
    tracked(cfg, "clean")
    tracked(cfg, "dirty", dirty=True)
    root = live_root(cfg)
    make_skill(str(root), "plain")
    text = lockfile.render_status(cfg)
    assert "共 3 个" in text and "本地有改动 1" in text and "未纳管 1" in text
    assert "aml patrol diff" in text


def test_status_render_handles_empty(tmp_path):
    cfg = base_cfg(tmp_path)
    assert "没有发现任何技能" in lockfile.render_status(cfg)
