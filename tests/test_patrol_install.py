"""安装/卸载 + 主机白名单 + 仓库缓存的测试（全离线：fake fetch，不联网）。

要钉死的口径：
  · plan 只读、apply 才动文件；`--dry-run` 走 plan 不动盘
  · 装到**作用域里的所有活目录**，`sync_kb: false` 的目录也照装（只是不进知识库）
  · 目标已有同名技能且本地改过 → 默认拦下（`--force` 才覆盖），覆盖/卸载**一定先备份**
  · 来源匹配用归一化精确匹配，不猜；找不到的技能如实报 missing
  · 主机白名单：非白名单 URL 直接拒绝，且说清怎么加
  · tarball 缓存：TTL 内不重复下载，解包到新临时目录（不会把缓存删掉）
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml.patrol import github, install, scopes, skills, sources  # noqa: E402


def make_skill(root, name, body="正文", extra=None):
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


class FakeFetch:
    """假的取快照：把本地目录当"远端仓库"，记录被调了几次（缓存测试用）。"""

    def __init__(self, root, ref="abc1234"):
        self.root = root
        self.ref = ref
        self.calls = 0

    def __call__(self, cfg, source, force_refresh=False):
        self.calls += 1
        return self.root, self.ref, None


def two_scopes_cfg(tmp_path):
    """一个 global（两目录：a 同步知识库、b 不同步）+ 一个项目作用域。"""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    cfg = make_cfg(tmp_path, candidate_repos=[], scopes_cfg=[
        {"name": "global", "kind": "global", "roots": [
            {"name": "a", "path": str(a), "sync_kb": True, "mirror_to": "技能原始/a"},
            {"name": "b", "path": str(b), "sync_kb": False}]},
        {"name": "project:proj", "kind": "project", "path": str(project),
         "roots": [{"name": "p", "path": "skills"}]}])
    return cfg, a, b, project


# ------------------------------------------------------------------ 安装

def test_install_into_all_live_roots_of_scope(tmp_path):
    cfg, a, b, _project = two_scopes_cfg(tmp_path)
    repo_root = tmp_path / "repo"
    make_skill(str(repo_root), "skills/tdd", body="新技能正文")
    sources.add(cfg, "owner/repo", log=lambda *_: None)
    plan = install.plan_install(cfg, ["tdd"], fetch=FakeFetch(str(repo_root)))
    assert plan["scope"] == "global" and {r["name"] for r in plan["roots"]} == {"a", "b"}
    assert plan["picks"]["tdd"]["subdir"] == "skills/tdd"
    report = install.apply_plan(cfg, plan, log=lambda *_: None)
    assert sorted(report["installed"]) == ["tdd@a", "tdd@b"]
    for root in (a, b):
        dest = root / "skills" / "tdd"
        assert "新技能正文" in (dest / "SKILL.md").read_text(encoding="utf-8")
        meta, _ = skills.read_meta(str(dest))
        assert meta["repo"] == "owner/repo" and meta["commit"] == "abc1234"
        assert meta["content_hash"] == meta["upstream_hash"] and meta["local_diff"] is False


def test_global_scope_skips_missing_roots_and_uses_existing(tmp_path):
    cfg = make_cfg(tmp_path, scopes_cfg=[{"name": "global", "kind": "global", "roots": [
        {"name": "missing", "path": str(tmp_path / "nope")},
        {"name": "live", "path": str(tmp_path / "live")}]}])
    os.makedirs(tmp_path / "live", exist_ok=True)
    repo_root = tmp_path / "repo"
    make_skill(str(repo_root), "skills/x")
    sources.add(cfg, "o/r", log=lambda *_: None)
    plan = install.plan_install(cfg, ["x"], fetch=FakeFetch(str(repo_root)))
    assert [r["name"] for r in plan["roots"]] == ["live"]


def test_project_scope_targets_project_dir(tmp_path):
    cfg, _a, _b, project = two_scopes_cfg(tmp_path)
    repo_root = tmp_path / "repo"
    make_skill(str(repo_root), "api-style")
    sources.add(cfg, "o/r", log=lambda *_: None)
    report = install.install(cfg, ["api-style"], project=str(project),
                             fetch=FakeFetch(str(repo_root)), log=lambda *_: None)
    assert report["scope"] == "project:proj"
    assert (project / "skills" / "api-style" / "SKILL.md").is_file()
    assert not (cfg.knowledge_dir / "技能原始").exists()      # 项目技能默认不进知识库


def test_install_matches_names_normalized_and_reports_missing(tmp_path):
    cfg, _a, _b, _project = two_scopes_cfg(tmp_path)
    repo_root = tmp_path / "repo"
    make_skill(str(repo_root), "skills/Skill-Name")
    sources.add(cfg, "o/r", log=lambda *_: None)
    plan = install.plan_install(cfg, ["skill_name", "nope"], fetch=FakeFetch(str(repo_root)))
    assert list(plan["picks"]) == ["skill_name"] and plan["missing"] == ["nope"]
    report = install.apply_plan(cfg, plan, log=lambda *_: None)
    assert report["missing"] == ["nope"]


def test_install_dry_run_touches_nothing(tmp_path):
    cfg, a, _b, _project = two_scopes_cfg(tmp_path)
    repo_root = tmp_path / "repo"
    make_skill(str(repo_root), "skills/x")
    sources.add(cfg, "o/r", log=lambda *_: None)
    report = install.install(cfg, ["x"], fetch=FakeFetch(str(repo_root)), dry_run=True,
                             log=lambda *_: None)
    assert report["dry_run"] and report["targets"]["x"] == ["a", "b"]
    assert not (a / "skills" / "x").exists()


def test_install_blocks_locally_modified_target_unless_forced(tmp_path):
    cfg, a, _b, _project = two_scopes_cfg(tmp_path)
    dest = a / "skills" / "x"
    make_skill(str(a / "skills"), "x", body="我自己改过的")
    skills.write_meta(str(dest), {"name": "x", "content_hash": "指纹不匹配", "local_diff": False})
    repo_root = tmp_path / "repo"
    make_skill(str(repo_root), "skills/x", body="上游新版")
    sources.add(cfg, "o/r", log=lambda *_: None)
    report = install.install(cfg, ["x"], fetch=FakeFetch(str(repo_root)), log=lambda *_: None)
    assert any(item.startswith("x@a") for item in report["blocked"])
    assert "我自己改过的" in (dest / "SKILL.md").read_text(encoding="utf-8")
    forced = install.install(cfg, ["x"], fetch=FakeFetch(str(repo_root)), force=True,
                             log=lambda *_: None)
    assert "x@a" in forced["replaced"]
    assert "上游新版" in (dest / "SKILL.md").read_text(encoding="utf-8")
    backups = list((cfg.state_dir / "patrol" / "_backup").iterdir())
    assert any(p.name.startswith("x-") for p in backups)     # 覆盖前一定备份过


def test_install_reports_source_errors_without_crashing(tmp_path):
    cfg, _a, _b, _project = two_scopes_cfg(tmp_path)
    sources.add(cfg, "o/broken", priority=1, log=lambda *_: None)
    sources.add(cfg, "o/good", priority=2, log=lambda *_: None)
    repo_root = tmp_path / "repo"
    make_skill(str(repo_root), "skills/x")

    def flaky(cfg_, source, force_refresh=False):
        if source["repo"] == "o/broken":
            raise RuntimeError("网络抖了")
        return str(repo_root), "abc1234", None

    plan = install.plan_install(cfg, ["x"], fetch=flaky)
    assert list(plan["picks"]) == ["x"]
    assert any(item.get("error") for item in plan["scanned"])


def test_install_requires_names_and_known_source(tmp_path):
    cfg, _a, _b, _project = two_scopes_cfg(tmp_path)
    try:
        install.plan_install(cfg, [])
        raise AssertionError("应当拒绝空技能名")
    except ValueError:
        pass
    try:
        install.plan_install(cfg, ["x"], source_key="no/such")
        raise AssertionError("应当拒绝未知来源")
    except KeyError as e:
        assert "sources add" in str(e)


# ------------------------------------------------------------------ 卸载

def test_uninstall_backs_up_then_removes(tmp_path):
    cfg, a, b, _project = two_scopes_cfg(tmp_path)
    for root in (a, b):
        make_skill(str(root), "x", body="要删的正文")
    report = install.uninstall(cfg, ["x", "ghost"], log=lambda *_: None)
    assert sorted(report["removed"]) == ["x@a", "x@b"] and report["missing"] == ["ghost"]
    assert not (a / "x").exists() and not (b / "x").exists()
    backups = [p for p in (cfg.state_dir / "patrol" / "_backup").iterdir()
               if p.name.startswith("x-")]
    assert len(backups) >= 2                                   # 两个目录各备份一份
    assert "要删的正文" in (backups[0] / "SKILL.md").read_text(encoding="utf-8")


def test_uninstall_dry_run_keeps_files(tmp_path):
    cfg, a, _b, _project = two_scopes_cfg(tmp_path)
    make_skill(str(a), "x")
    report = install.uninstall(cfg, ["x"], dry_run=True, log=lambda *_: None)
    assert report["removed"] == ["x@a"] and (a / "x" / "SKILL.md").is_file()


# ------------------------------------------------------------------ 白名单与缓存

def test_host_allowlist_rejects_unknown_host(tmp_path):
    cfg = make_cfg(tmp_path)
    assert github.check_url("https://github.com/a/b", cfg) == "github.com"
    try:
        github.check_url("https://gitlab.com/a/b", cfg)
        raise AssertionError("应当拒绝非白名单主机")
    except PermissionError as e:
        assert "allowed_hosts" in str(e)
    cfg.data["patrol"]["allowed_hosts"] = ["github.com", "gitlab.com"]
    assert github.check_url("https://gitlab.com/a/b", cfg) == "gitlab.com"


def test_repo_identifier_must_not_be_a_url():
    try:
        github.check_repo("https://github.com/a/b")
        raise AssertionError("应当拒绝 URL 当仓库标识")
    except ValueError as e:
        assert "owner/repo" in str(e)
    assert github.check_repo("a/b") == "a/b"


def test_tarball_cache_hits_within_ttl_and_force_refresh_skips(tmp_path):
    cfg = make_cfg(tmp_path, cache_ttl_sec=900)
    path = github.cache_path(cfg, "a/b", "main")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x")
    info = github.cache_info(cfg, "a/b", "main")
    assert info["hit"] is True and info["age"] is not None
    os.utime(path, (time.time() - 4000, time.time() - 4000))
    assert github.cache_info(cfg, "a/b", "main")["hit"] is False      # 过期
    cfg.data["patrol"]["cache_ttl_sec"] = 0
    assert github.cache_info(cfg, "a/b", "main")["hit"] is False      # 关掉缓存


def test_prune_cache_removes_only_old_files(tmp_path):
    cfg = make_cfg(tmp_path)
    root = cfg.state_dir / "patrol" / "_cache" / "a__b"
    root.mkdir(parents=True, exist_ok=True)
    old, new = root / "old.tar.gz", root / "new.tar.gz"
    old.write_bytes(b"1"), new.write_bytes(b"2")
    os.utime(old, (time.time() - 30 * 86400, time.time() - 30 * 86400))
    assert github.prune_cache(cfg, keep_days=14) == 1
    assert new.is_file() and not old.is_file()


# ------------------------------------------------------------------ 渲染

def test_render_plan_lists_targets_sources_and_missing(tmp_path):
    cfg, _a, _b, _project = two_scopes_cfg(tmp_path)
    repo_root = tmp_path / "repo"
    make_skill(str(repo_root), "skills/tdd")
    sources.add(cfg, "o/r", log=lambda *_: None)
    plan = install.plan_install(cfg, ["tdd", "ghost"], fetch=FakeFetch(str(repo_root)))
    text = install.render_plan(plan)
    assert "安装计划" in text and "tdd" in text and "ghost" in text
    assert "同步知识库：a" in text and "o/r" in text


def test_scopes_render_shows_project_scope(tmp_path):
    cfg, _a, _b, _project = two_scopes_cfg(tmp_path)
    text = scopes.render(cfg)
    assert "project:proj" in text and "只在本地" in text
