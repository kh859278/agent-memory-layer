"""自查更新的归属校验与 git 源测试（不联网：全部注入假的 fetch / run）。

要钉死的事实（这是实测踩出来的，不是假想）：
  · PyPI 上的 `agent-memory-layer` **不是本项目**（SAP 的包）→ 必须拒绝，且不给版本号
  · 归属校验不过时 `auto` 要**退回查我们自己仓库的 tag**，并把原因说明白
  · 有归属标记（project_urls 指向我们的仓库）时正常返回版本
  · git tag 解析：取版本最大的一个；解析不到就报错，不拿 HEAD 冒充版本
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import selfupdate  # noqa: E402

FOREIGN = {"info": {"version": "0.1.1",
                    "summary": "A reusable memory layer for SAP agentic workflows",
                    "project_urls": {"Changelog": "https://github.tools.sap/I751606/x"}}}
OURS = {"info": {"version": "0.2.0", "summary": "Cross-agent memory layer",
                 "project_urls": {"Repository": "https://github.com/kh859278/agent-memory-layer"}}}


def fetch_returning(payload):
    return lambda url: payload


def git_returning(output, rc=0):
    return lambda command: (rc, output)


TAGS = "\n".join([
    "aaa111\trefs/tags/v0.1.0",
    "bbb222\trefs/tags/v0.1.2",
    "ccc333\trefs/tags/v0.1.2^{}",
    "ddd444\trefs/tags/v0.1.1",
])


def test_pypi_foreign_package_is_rejected():
    result = selfupdate.latest_version(fetch=fetch_returning(FOREIGN))
    assert result["version"] is None and result["foreign"] is True
    assert "不是本项目" in result["error"] and "SAP" in result["error"]


def test_ownership_accepts_our_repo_marker():
    assert selfupdate.ownership(OURS["info"])["ours"] is True
    assert selfupdate.ownership(FOREIGN["info"])["ours"] is False
    assert selfupdate.ownership({})["ours"] is False


def test_own_package_returns_version():
    result = selfupdate.latest_version(fetch=fetch_returning(OURS))
    assert result["version"] == "0.2.0" and not result.get("foreign")


def test_auto_falls_back_to_git_when_pypi_is_foreign():
    result = selfupdate.resolve_latest(fetch=fetch_returning(FOREIGN),
                                       run=git_returning(TAGS), source="auto")
    assert result["source"] == "git" and result["version"] == "0.1.2"
    assert "不是本项目" in (result.get("note") or "")


def test_pypi_source_does_not_fall_back():
    result = selfupdate.resolve_latest(fetch=fetch_returning(FOREIGN),
                                       run=git_returning(TAGS), source="pypi")
    assert result["source"] == "pypi" and result["version"] is None and result["foreign"]


def test_git_latest_picks_highest_tag_and_strips_v():
    result = selfupdate.git_latest(run=git_returning(TAGS))
    assert result["version"] == "0.1.2" and result["error"] is None


def test_git_latest_reports_error_when_no_tags_or_failure():
    empty = selfupdate.git_latest(run=git_returning(""))
    assert empty["version"] is None and "还没有 tag" in empty["error"]
    failed = selfupdate.git_latest(run=git_returning("boom", rc=128))
    assert failed["version"] is None and "ls-remote 失败" in failed["error"]


def test_git_latest_swallows_network_errors():
    def boom(command):
        raise TimeoutError("网络挂了")

    result = selfupdate.git_latest(run=boom)
    assert result["version"] is None and "网络挂了" in result["error"]


def test_plan_reports_source_and_foreign_flag():
    plan = selfupdate.plan(prefix="C:/Python312", fetch=fetch_returning(FOREIGN),
                           run=git_returning(TAGS))
    assert plan["source"] == "git" and plan["latest"] == "0.1.2" and plan["foreign"] is False
    text = selfupdate.render(plan)
    assert "git tag" in text and "不是本项目" in text


def test_plan_with_pypi_source_never_suggests_upgrade_for_foreign_package():
    plan = selfupdate.plan(prefix="C:/Python312", fetch=fetch_returning(FOREIGN), source="pypi")
    assert plan["latest"] is None and plan["needs_upgrade"] is False
    assert plan["foreign"] is True


def test_remote_repo_defaults_and_config_override(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    assert selfupdate.remote_repo(cfg).endswith("kh859278/agent-memory-layer.git")
    cfg.data["patrol"]["selfupdate"] = {"repo": "https://github.com/me/fork.git"}
    assert selfupdate.remote_repo(cfg) == "https://github.com/me/fork.git"


def test_dry_run_never_executes_with_foreign_package():
    calls = []
    result = selfupdate.apply(prefix="C:/Python312", dry_run=True,
                              fetch=fetch_returning(FOREIGN), run=git_returning(TAGS),
                              log=lambda *_: None)
    assert result["dry_run"] is True and calls == []
    assert result["needs_upgrade"] is False
