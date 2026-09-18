"""作用域（装到哪）与来源（从哪装）的测试：全部离线，只碰临时目录。

要钉死的口径：
  · 老配置（只有 `skill_roots`）零改动可用：自动派生成一个 global 作用域
  · `sync_kb: false` 的目录照样被治理，但**不进知识库**（mirror 只看 kb_roots）
  · 项目作用域里的相对路径按项目根解析；项目技能默认不同步知识库
  · 仓库布局识别认得 root / standard / template / flat / nested 五种
  · 来源文件一旦落盘就是唯一真相（不再回头看配置）
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml.patrol import scopes, skills, sources  # noqa: E402


def make_skill(root, name, body="正文", extra=None):
    """在 root/<name> 造一个技能；front-matter 里的 name 用**目录名**（与真实技能一致）。"""
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    stem = os.path.basename(name.rstrip("/\\"))
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {stem}\ndescription: 测试技能 {stem}\n---\n\n{body}\n")
    for rel, content in (extra or {}).items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    return path


def make_cfg(tmp_path, roots=None, scopes_cfg=None, **patrol):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["skill_roots"] = roots if roots is not None else []
    if scopes_cfg is not None:
        cfg.data["patrol"]["scopes"] = scopes_cfg
    cfg.data["patrol"].update(patrol)
    return cfg


# ------------------------------------------------------------------ 作用域

def test_legacy_skill_roots_derive_a_global_scope(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "s1")
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "技能原始/old"}])
    derived = scopes.load_scopes(cfg)
    assert [s["name"] for s in derived] == ["global"] and derived[0]["derived"] is True
    rows = scopes.summary(cfg)
    assert rows[0]["name"] == "old" and rows[0]["sync_kb"] is True and rows[0]["skills"] == 1


def test_sync_kb_false_directory_is_governed_but_not_mirrored(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    make_skill(str(a), "shared")
    make_skill(str(b), "local-only")
    cfg = make_cfg(tmp_path, scopes_cfg=[{
        "name": "global", "kind": "global", "roots": [
            {"name": "a", "path": str(a), "sync_kb": True, "mirror_to": "技能原始/a"},
            {"name": "b", "path": str(b), "sync_kb": False}]}])
    assert [n for n, _p, _m in skills.live_roots(cfg)] == ["a", "b"]      # 都被治理
    assert [n for n, _p, _m in skills.kb_roots(cfg)] == ["a"]             # 只有 a 进知识库
    report = skills.mirror(cfg)
    assert list(report) == ["a"]
    assert not (cfg.knowledge_dir / "技能原始" / "b").exists()


def test_project_scope_resolves_relative_roots_and_defaults_to_no_kb(tmp_path):
    project = tmp_path / "proj"
    make_skill(str(project / ".agents" / "skills"), "s1")
    cfg = make_cfg(tmp_path, scopes_cfg=[{
        "name": "project:proj", "kind": "project", "path": str(project),
        "roots": [{"name": "project-skills", "path": ".agents/skills"}]}])
    rows = scopes.summary(cfg)
    assert os.path.samefile(rows[0]["path"], str(project / ".agents" / "skills"))
    assert rows[0]["sync_kb"] is False and rows[0]["skills"] == 1
    assert scopes.for_path(cfg, str(project / "src" / "x.py"))["name"] == "project:proj"
    assert scopes.for_path(cfg, str(tmp_path / "elsewhere")) is None


def test_project_scope_can_opt_into_kb_sync(tmp_path):
    project = tmp_path / "proj"
    make_skill(str(project / "skills"), "s1")
    cfg = make_cfg(tmp_path, scopes_cfg=[{
        "name": "project:proj", "kind": "project", "path": str(project), "roots": [
            {"name": "p", "path": "skills", "sync_kb": True, "mirror_to": "技能原始/p"}]}])
    assert [r["mirror_to"] for r in scopes.kb_roots(cfg)] == ["技能原始/p"]


def test_resolve_prefers_named_scope_then_project_then_global(tmp_path):
    cfg = make_cfg(tmp_path, scopes_cfg=[
        {"name": "global", "kind": "global", "roots": []},
        {"name": "project:x", "kind": "project", "path": str(tmp_path / "x"), "roots": []}])
    assert scopes.resolve(cfg, "project:x")["name"] == "project:x"
    assert scopes.resolve(cfg, project=str(tmp_path / "x" / "sub"))["name"] == "project:x"
    assert scopes.resolve(cfg)["name"] == "global"
    try:
        scopes.resolve(cfg, "nope")
        raise AssertionError("应当报错")
    except KeyError as e:
        assert "现有" in str(e)


def test_longest_project_path_wins(tmp_path):
    cfg = make_cfg(tmp_path, scopes_cfg=[
        {"name": "project:outer", "kind": "project", "path": str(tmp_path), "roots": []},
        {"name": "project:inner", "kind": "project", "path": str(tmp_path / "inner"), "roots": []}])
    assert scopes.for_path(cfg, str(tmp_path / "inner" / "a"))["name"] == "project:inner"
    assert scopes.for_path(cfg, str(tmp_path / "other"))["name"] == "project:outer"


def test_scope_render_lists_state_and_hint(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "s1")
    cfg = make_cfg(tmp_path, [{"name": "live", "path": str(live)}])
    text = scopes.render(cfg)
    assert "global" in text and "已同步知识库" in text and "派生自 skill_roots" in text


# ------------------------------------------------------------------ 来源

def test_sources_are_seeded_from_config_then_file_wins(tmp_path):
    cfg = make_cfg(tmp_path, candidate_repos=["owner/one"])
    seeded = sources.load(cfg)
    assert [s["repo"] for s in seeded] == ["owner/one"] and seeded[0]["seed"] == "candidate_repos"
    assert sources.default_path(cfg).is_file()
    sources.add(cfg, "owner/two", log=lambda *_: None)
    cfg.data["patrol"]["candidate_repos"] = ["owner/three"]     # 配置再改也不影响已落盘的来源
    assert sorted(s["repo"] for s in sources.load(cfg)) == ["owner/one", "owner/two"]


def test_source_add_update_remove_and_enable(tmp_path):
    cfg = make_cfg(tmp_path, candidate_repos=[])
    sources.add(cfg, "a/b", layout="standard", priority=10, log=lambda *_: None)
    sources.add(cfg, "a/b", subdir="skills/x", log=lambda *_: None)      # 同 repo 不同 subdir = 新来源
    assert len(sources.load(cfg)) == 2
    assert sources.set_enabled(cfg, "a/b", False, log=lambda *_: None)
    assert [s["enabled"] for s in sources.load(cfg) if s["repo"] == "a/b"] == [False, False]
    # 带 # 的键精确到子目录：只删那一个
    assert sources.remove(cfg, "a/b#skills/x", log=lambda *_: None)
    remaining = sources.load(cfg)
    assert [sources.key_of(s) for s in remaining] == ["a/b"]
    # 只给 repo：这个仓库的全部子来源一起删
    assert sources.remove(cfg, "a/b", log=lambda *_: None)
    assert sources.load(cfg) == []
    try:
        sources.add(cfg, "a/b", layout="nonsense", log=lambda *_: None)
        raise AssertionError("应当拒绝未知 layout")
    except ValueError as e:
        assert "layout" in str(e)


def test_enabled_sources_sorted_by_priority_and_scope(tmp_path):
    cfg = make_cfg(tmp_path, candidate_repos=[])
    sources.add(cfg, "late/repo", priority=200, log=lambda *_: None)
    sources.add(cfg, "early/repo", priority=5, log=lambda *_: None)
    sources.add(cfg, "other/scope", scope="project:p", priority=1, log=lambda *_: None)
    assert [s["repo"] for s in sources.enabled_sources(cfg)] == ["other/scope", "early/repo",
                                                                "late/repo"]
    assert [s["repo"] for s in sources.enabled_sources(cfg, scope="global")] == ["early/repo",
                                                                                "late/repo"]


def test_classify_covers_the_five_layouts():
    assert sources.classify("") == "root"
    assert sources.classify("skill-a") == "flat"
    assert sources.classify("skills/skill-a") == "standard"
    assert sources.classify("template/skill-a") == "template"
    assert sources.classify("plugins/x/skills/skill-a") == "nested"
    assert sources.classify("a/b") == "nested"


def test_detect_layouts_over_a_mixed_repo(tmp_path):
    root = tmp_path / "repo"
    make_skill(str(root), "skills/standard-one")            # standard
    make_skill(str(root), "template/tpl-one")               # template
    make_skill(str(root), "flat-one")                       # flat
    make_skill(str(root), "plugins/pkg/skills/nested-one")  # nested
    report = sources.detect_layouts(str(root))
    assert report["layouts"] == {"standard": 1, "template": 1, "flat": 1, "nested": 1}
    names = {s["name"] for s in report["skills"]}
    assert names == {"standard-one", "tpl-one", "flat-one", "nested-one"}


def test_detect_layouts_handles_repo_root_as_one_skill(tmp_path):
    root = tmp_path / "repo"
    make_skill(str(tmp_path), "repo")           # 仓库根就是一个技能
    report = sources.detect_layouts(str(root))
    assert report["layouts"] == {"root": 1} and report["skills"][0]["subdir"] == ""


def test_enumerate_source_respects_layout_and_subdir(tmp_path):
    root = tmp_path / "repo"
    make_skill(str(root), "skills/one")
    make_skill(str(root), "plugins/pkg/skills/two")
    auto = sources.enumerate_source({"repo": "r", "layout": "auto"}, str(root))
    assert set(auto) == {"one", "two"}
    only_std = sources.enumerate_source({"repo": "r", "layout": "standard"}, str(root))
    assert list(only_std) == ["one"]
    sub = sources.enumerate_source({"repo": "r", "subdir": "plugins/pkg"}, str(root))
    assert list(sub) == ["two"] and sub["two"] == "plugins/pkg/skills/two"


def test_match_plan_normalizes_names_instead_of_guessing(tmp_path):
    mapping = {"Skill-Name": "skills/a", "other": "skills/b"}
    plan = sources.match_plan(["skill_name", "missing"], mapping)
    assert plan == {"skill_name": "skills/a"}


def test_sources_render_reports_state(tmp_path):
    cfg = make_cfg(tmp_path, candidate_repos=["owner/repo"])
    sources.add(cfg, "owner/other", layout="nested", subdir="plugins/x", priority=20, log=print)
    sources.set_enabled(cfg, "owner/other", False, log=lambda *_: None)
    text = sources.render(cfg)
    assert "启用" in text and "暂停" in text and "layout=nested" in text
    assert "owner/repo" in text and "p20" in text


def test_render_layouts_shows_counts_and_paths(tmp_path):
    root = tmp_path / "repo"
    make_skill(str(root), "skills/one")
    text = sources.render_layouts(sources.detect_layouts(str(root)), repo="owner/repo", head="abc123")
    assert "owner/repo@abc123" in text and "standard" in text and "skills/one" in text


def test_sources_json_is_valid_and_stable(tmp_path):
    cfg = make_cfg(tmp_path, candidate_repos=[])
    sources.add(cfg, "a/b", log=lambda *_: None)
    data = json.loads(sources.default_path(cfg).read_text(encoding="utf-8"))
    assert data["sources"][0]["repo"] == "a/b" and "updated_at" in data
