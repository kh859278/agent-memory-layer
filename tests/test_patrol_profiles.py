"""profile 与配置同步的测试（离线：远端用本地路径，git 传输用假 runner）。

要钉死的口径：
  · profile 是**非交互**复现清单：{技能名: 来源} + 作用域，按来源分组装
  · `--profile` 装整套时按来源分组（少下载几遍仓库），并汇总各来源的结果
  · 拉取合并：远端新 profile → 加；同名不同内容 → **不覆盖**，报冲突让人决定
  · 推送/拉取支持"本地路径"与"git 仓库"两种传输；HTTPS 远端受主机白名单约束
  · git 传输用假 runner 验证：clone → 写文件 → add → commit → push（顺序不能少）
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml.patrol import install, profiles  # noqa: E402


def make_skill(root, name):
    """造技能：front-matter 的 name 用**目录名**（与真实技能一致）。

    第一版直接写 `name: skills/alpha`（把子目录路径当成了技能名），于是来源匹配
    `norm("skills/alpha") != norm("alpha")`，全部判成 missing —— 测试自己的 bug，
    而且伪装成"代码没装上"，很值得留这条注释。
    """
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    stem = os.path.basename(name.rstrip("/\\"))
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {stem}\ndescription: 测试技能 {stem}\n---\n\n正文\n")
    return path


def make_cfg(tmp_path, scopes_cfg=None):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    live = tmp_path / "live"
    live.mkdir(parents=True, exist_ok=True)
    cfg.data["patrol"]["skill_roots"] = []
    cfg.data["patrol"]["scopes"] = scopes_cfg or [{
        "name": "global", "kind": "global",
        "roots": [{"name": "live", "path": str(live), "sync_kb": False}]}]
    return cfg


def tracked(cfg, name, repo="owner/repo"):
    from aml.patrol import skills
    live = cfg.section("patrol")["scopes"][0]["roots"][0]["path"]
    path = make_skill(live, name)
    skills.write_meta(path, {"name": name, "repo": repo, "subdir": f"skills/{name}",
                             "commit": "abc1234", "content_hash": skills.dir_hash(path),
                             "local_diff": False})
    return path


# ------------------------------------------------------------------ profile

def test_profile_put_get_remove_roundtrip(tmp_path):
    cfg = make_cfg(tmp_path)
    profiles.put(cfg, "laptop", "global", {"tdd": "mattpocock/skills",
                                           "grilling": "mattpocock/skills"}, note="笔记本",
                 log=lambda *_: None)
    profile = profiles.get(cfg, "laptop")
    assert profile["scope"] == "global" and profile["note"] == "笔记本"
    assert sorted(profiles.groups(profile)) == ["mattpocock/skills"]
    assert sorted(profiles.groups(profile)["mattpocock/skills"]) == ["grilling", "tdd"]
    assert profiles.default_path(cfg).is_file()
    assert profiles.remove(cfg, "laptop", log=lambda *_: None) is True
    assert profiles.load(cfg)["profiles"] == {}
    try:
        profiles.get(cfg, "laptop")
        raise AssertionError("删掉之后应当取不到")
    except KeyError as e:
        assert "现有" in str(e)


def test_profile_snapshot_uses_installed_skills(tmp_path):
    cfg = make_cfg(tmp_path)
    tracked(cfg, "tdd")
    tracked(cfg, "grilling")
    make_skill(cfg.section("patrol")["scopes"][0]["roots"][0]["path"], "untracked-one")
    profile = profiles.snapshot(cfg, "here", log=lambda *_: None)
    assert sorted(profile["skills"]) == ["grilling", "tdd"]      # 未纳管的不进 profile
    assert set(profile["skills"].values()) == {"owner/repo"}


def test_profile_render_and_empty(tmp_path):
    cfg = make_cfg(tmp_path)
    assert "还没有 profile" in profiles.render(cfg)
    profiles.put(cfg, "laptop", "global", {"tdd": "owner/repo"}, log=lambda *_: None)
    text = profiles.render(cfg)
    assert "laptop" in text and "owner/repo" in text and "install --profile" in text


def test_install_from_profile_groups_by_source(tmp_path):
    """profile 装整套：按来源分组（同来源只取一次快照），并汇总每个来源的结果。"""
    cfg = make_cfg(tmp_path)
    profiles.put(cfg, "laptop", "global", {"alpha": "owner/one", "beta": "owner/two"},
                 log=lambda *_: None)
    from aml.patrol import sources
    sources.add(cfg, "owner/one", log=lambda *_: None)
    sources.add(cfg, "owner/two", log=lambda *_: None)
    fetched = []

    def fake_fetch(cfg_, source, force_refresh=False):
        fetched.append(source["repo"])
        root = tmp_path / f"snap-{source['repo'].replace('/', '-')}"
        name = "alpha" if source["repo"] == "owner/one" else "beta"
        make_skill(str(root), "skills/" + name)
        return str(root), "abc1234", None

    report = install.install_from_profile(cfg, "laptop", fetch=fake_fetch, log=lambda *_: None)
    assert sorted(report["installed"]) == ["alpha@live", "beta@live"], report
    assert [g["source"] for g in report["groups"]] == ["owner/one", "owner/two"]
    assert fetched == ["owner/one", "owner/two"]
    live = cfg.section("patrol")["scopes"][0]["roots"][0]["path"]
    assert os.path.isdir(os.path.join(live, "skills", "alpha"))
    assert os.path.isdir(os.path.join(live, "skills", "beta"))


def test_install_from_profile_dry_run_and_missing(tmp_path):
    cfg = make_cfg(tmp_path)
    profiles.put(cfg, "laptop", "global", {"ghost": "owner/one"}, log=lambda *_: None)
    from aml.patrol import sources
    sources.add(cfg, "owner/one", log=lambda *_: None)

    def fake_fetch(cfg_, source, force_refresh=False):
        root = tmp_path / "empty-snap"
        os.makedirs(root, exist_ok=True)
        return str(root), "abc1234", None

    report = install.install_from_profile(cfg, "laptop", dry_run=True, fetch=fake_fetch,
                                          log=lambda *_: None)
    assert report["missing"] == ["ghost"] and report["installed"] == []
    with_plan = install.install_from_profile(cfg, "laptop", fetch=fake_fetch, log=lambda *_: None)
    assert with_plan["missing"] == ["ghost"]


def test_remote_path_push_then_pull_merges_without_overwrite(tmp_path):
    cfg = make_cfg(tmp_path)
    profiles.put(cfg, "laptop", "global", {"tdd": "owner/repo"}, log=lambda *_: None)
    remote = tmp_path / "shared" / "aml-profiles.json"
    result = profiles.push(cfg, str(remote), log=lambda *_: None)
    assert result["ok"] and remote.is_file()
    data = json.loads(remote.read_text(encoding="utf-8"))
    assert data["version"] == 1 and "laptop" in data["profiles"]

    other = make_cfg(tmp_path / "other")
    merged = profiles.pull(other, str(remote), log=lambda *_: None)
    assert merged["ok"] and merged["added"] == ["laptop"]
    assert profiles.get(other, "laptop")["skills"] == {"tdd": "owner/repo"}


def test_pull_reports_conflict_without_overwriting(tmp_path):
    cfg = make_cfg(tmp_path)
    profiles.put(cfg, "laptop", "global", {"tdd": "owner/repo"}, log=lambda *_: None)
    remote = tmp_path / "aml-profiles.json"
    remote.write_text(json.dumps({"version": 1, "profiles": {
        "laptop": {"scope": "global", "skills": {"tdd": "someone/else"}},
        "desktop": {"scope": "global", "skills": {"grilling": "owner/repo"}}}}),
        encoding="utf-8")
    result = profiles.pull(cfg, str(remote), log=lambda *_: None)
    assert result["conflicts"] == ["laptop"] and result["added"] == ["desktop"]
    assert profiles.get(cfg, "laptop")["skills"] == {"tdd": "owner/repo"}   # 本地那份没被动


def test_push_pull_dry_run_do_not_touch_remote(tmp_path):
    cfg = make_cfg(tmp_path)
    profiles.put(cfg, "laptop", "global", {"tdd": "owner/repo"}, log=lambda *_: None)
    remote = tmp_path / "nope" / "aml-profiles.json"
    result = profiles.push(cfg, str(remote), dry_run=True, log=lambda *_: None)
    assert result["dry_run"] and not remote.exists()
    missing = profiles.pull(cfg, str(remote), log=lambda *_: None)
    assert missing["ok"] is False and "找不到文件" in missing["error"]


def test_git_transport_runs_clone_commit_push(tmp_path):
    cfg = make_cfg(tmp_path)
    profiles.put(cfg, "laptop", "global", {"tdd": "owner/repo"}, log=lambda *_: None)
    calls = []

    def fake_run(args, cwd=None):
        calls.append(list(args))
        if args and args[0] == "clone":
            os.makedirs(args[-1], exist_ok=True)
            with open(os.path.join(args[-1], "README.md"), "w", encoding="utf-8") as f:
                f.write("remote repo\n")
        return 0, "ok"

    result = profiles.push(cfg, "https://github.com/owner/profiles.git", run=fake_run,
                           log=lambda *_: None)
    assert result["ok"] is True
    flat = [" ".join(c) for c in calls]
    assert any(c.startswith("clone") for c in flat)
    assert any("add" in c and profiles.PROFILE_FILE in c for c in flat)
    assert any("push" in c for c in flat)


def test_git_transport_respects_host_allowlist(tmp_path):
    cfg = make_cfg(tmp_path)
    try:
        profiles.push(cfg, "https://gitlab.com/owner/profiles.git", run=lambda *a: (0, ""),
                      log=lambda *_: None)
        raise AssertionError("非白名单主机应当被拒绝")
    except PermissionError as e:
        assert "allowed_hosts" in str(e)


def test_is_git_remote_classification():
    assert profiles.is_git_remote("https://github.com/a/b.git")
    assert profiles.is_git_remote("git@github.com:a/b.git")
    assert not profiles.is_git_remote("C:/<home>/profiles.json")
    assert not profiles.is_git_remote("file:///tmp/x.json")
