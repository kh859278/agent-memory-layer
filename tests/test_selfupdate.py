"""自查更新测试：全部离线 —— `fetch` / `run` 一律注入假的。

要钉死的口径：
  · 版本比较**容错**：`0.1.0rc1` / `v1.2` / 垃圾串都不能抛异常
  · 三种安装方式（pipx / uv / pip）按路径识别；uv **优先于** pipx
  · 取不到远端 = `error` 字符串 + `needs_upgrade=False`（查不到 ≠ 要升级），**绝不抛**
  · `dry_run=True` 时一个进程都不起（这是本模块的默认路径，必须钉死）
  · 真执行时 `rc != 0` 要如实返回失败，不能假装成功

注意：这里**不允许**出现真实网络调用。默认 `fetch`（urllib）只被 import，不会被调到。
"""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from aml import selfupdate


def fake_fetch(version="9.9.9"):
    """假索引：返回 PyPI JSON 的形状。

    **必须带上我们自己的仓库链接**：`latest_version` 会校验归属（PyPI 上的同名包是 SAP 的，
    详见 `test_selfupdate_ownership.py`）。不带标记的 payload 会被当成"别人的包"拒绝，
    这里的用例就变成在测另一件事了。仓库链接缺失的用例在 ownership 那个文件里。
    """
    return lambda url: {"info": {"version": version, "name": "aml-memory",
                                 "project_urls": {
                                     "Repository": "https://github.com/kh859278/agent-memory-layer"}}}


def boom_fetch(exc=None):
    def _fetch(url):
        raise exc or OSError("network down")
    return _fetch


# ------------------------------------------------------------------ 版本比较容错

def test_version_key_tolerates_prerelease_and_prefixes():
    assert selfupdate.version_key("0.1.0rc1") == (0, 1, 0, 1)
    assert selfupdate.version_key("v1.2.3") == (1, 2, 3)
    assert selfupdate.version_key("2.0") == (2, 0)
    assert selfupdate.version_key("0.1.0+local") == (0, 1, 0)
    assert selfupdate.version_key("") == ()
    assert selfupdate.version_key(None) == ()


def test_version_key_garbage_does_not_raise():
    for bad in ("not-a-version", "abc", "....", "rc", " "):
        assert selfupdate.version_key(bad) == ()


def test_needs_upgrade_only_when_remote_is_higher():
    assert selfupdate.needs_upgrade("0.1.0", "0.1.1") is True
    assert selfupdate.needs_upgrade("0.1.0", "0.2.0") is True
    assert selfupdate.needs_upgrade("0.1.0", "0.1.0") is False
    assert selfupdate.needs_upgrade("0.2.0", "0.1.9") is False
    assert selfupdate.needs_upgrade(None, "0.1.0") is True       # 没装成包时也认得出"有新版"
    assert selfupdate.needs_upgrade("0.1.0", None) is False      # 查不到远端 → 不动


def test_prerelease_is_not_treated_as_upgrade():
    """已知取舍：数字口径下 `0.1.0` 与 `0.1.0rc1` 相等 → 不报升级（宁可漏报预发布）。"""
    assert selfupdate.needs_upgrade("0.1.0", "0.1.0rc1") is False


# ------------------------------------------------------------------ 安装方式识别

def test_install_method_detects_pipx():
    # 占位符不能写成真实用户目录（`C:/Users/<名>`）——泄漏扫描会当成真路径拦下，
    # 所以这里统一用 `you` 这种明显的占位值
    assert selfupdate.install_method(prefix="C:/<home>/pipx/venvs/aml-memory") == "pipx"
    assert selfupdate.install_method(prefix="/home/you/.local/pipx/venvs/aml") == "pipx"


def test_install_method_detects_uv():
    assert selfupdate.install_method(prefix="C:/<home>/AppData/Roaming/uv/tools/aml") == "uv"
    assert selfupdate.install_method(prefix="/home/you/.local/share/uv/tools/aml") == "uv"


def test_install_method_uv_wins_over_pipx():
    """`uv tool install pipx` 这种套娃：被管理的是 uv 的环境，pipx 命令不对。"""
    assert selfupdate.install_method(prefix="C:/uv/tools/pipx") == "uv"


def test_install_method_defaults_to_pip_for_plain_python():
    assert selfupdate.install_method(prefix="C:/Python312") == "pip"
    assert selfupdate.install_method(prefix="/usr/lib/python3.9") == "pip"


def test_install_method_unknown_when_nothing_to_go_on():
    assert selfupdate.install_method(prefix="", executable="") == "unknown"


def test_install_method_explicit_prefix_ignores_local_prefix():
    """显式给了 prefix 就不能再混进本机真实 `sys.prefix` —— 否则测试结果取决于这台机器。"""
    assert selfupdate.install_method(prefix="C:/Python312", executable="C:/Python312/python.exe") == "pip"


# ------------------------------------------------------------------ 命令

def test_command_for_three_methods():
    pip_cmd = selfupdate.command_for("pip", prefix="C:/Python312")
    assert pip_cmd[0] == sys.executable
    assert pip_cmd[1:3] == ["-m", "pip"]
    assert pip_cmd[-3:] == ["install", "--upgrade", "aml-memory"]
    assert selfupdate.command_for("pipx") == ["pipx", "upgrade", "aml-memory"]
    assert selfupdate.command_for("uv") == ["uv", "tool", "upgrade", "aml-memory"]
    assert selfupdate.command_for("unknown") == []


def test_pip_command_uses_user_flag_for_system_python():
    """裸 pip 装进系统 Python 要 `--user`（否则 Windows 上要管理员）。"""
    cmd = selfupdate.command_for("pip", prefix="C:/Python312")
    assert "--user" in cmd
    assert cmd.index("--user") < cmd.index("install")


# ------------------------------------------------------------------ 查远端（失败绝不抛）

def test_latest_version_ok():
    got = selfupdate.latest_version(fetch=fake_fetch("1.4.2"))
    assert got["version"] == "1.4.2" and got["error"] is None
    assert got["url"].endswith("/aml-memory/json")


def test_latest_version_error_does_not_raise():
    got = selfupdate.latest_version(fetch=boom_fetch())
    assert got["version"] is None and "OSError" in got["error"]


def test_latest_version_handles_non_json_payload():
    got = selfupdate.latest_version(fetch=lambda url: {"unexpected": True})
    assert got["version"] is None and got["error"]


def test_latest_version_handles_weird_payload():
    got = selfupdate.latest_version(fetch=lambda url: "不是 dict")
    assert got["version"] is None and got["error"]


def test_index_url_default_and_override(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    assert selfupdate.index_url(cfg) == selfupdate.DEFAULT_INDEX      # 默认 PyPI
    cfg.data["patrol"]["selfupdate"] = {"index_url": "https://mirror.internal/pypi/"}
    assert selfupdate.index_url(cfg) == "https://mirror.internal/pypi"
    assert selfupdate.index_url(None) == selfupdate.DEFAULT_INDEX     # 没配置也不能炸


def test_latest_version_uses_configured_index(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["selfupdate"] = {"index_url": "https://mirror.internal/pypi"}
    seen = {}

    def fake(url):
        seen["url"] = url
        return {"info": {"version": "1.0.0", "project_urls": {
            "Repository": "https://github.com/kh859278/agent-memory-layer"}}}

    selfupdate.latest_version(cfg, fetch=fake)
    assert seen["url"] == "https://mirror.internal/pypi/aml-memory/json"


# ------------------------------------------------------------------ plan

def test_plan_marks_upgrade_with_command(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    report = selfupdate.plan(cfg, prefix="C:/Python312", fetch=fake_fetch("99.0.0"))
    assert report["method"] == "pip"
    assert report["latest"] == "99.0.0"
    assert report["needs_upgrade"] is True
    assert report["command"][-3:] == ["install", "--upgrade", "aml-memory"]
    assert report["error"] is None


def test_plan_no_upgrade_when_index_fails(tmp_path):
    """查不到远端 → 不升级，但仍给出"该怎么升级"的命令。

    `source="pypi"` 是**刻意固定**的：默认 `auto` 会退到 git tag 兜底（真实网络调用），
    本仓库打上 `v0.1.0` 之后兜底就会成功，这条断言随之失效
    （2026-09-22 实测：发布 + 打 tag 之后这条从绿变红）。
    """
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    report = selfupdate.plan(cfg, prefix="C:/Python312", fetch=boom_fetch(), source="pypi")
    assert report["needs_upgrade"] is False
    assert report["latest"] is None
    assert report["error"]
    assert report["command"]          # 查不到远端不影响"该怎么升级"这条信息


# ------------------------------------------------------------------ apply

def test_dry_run_executes_nothing(tmp_path):
    """`dry_run=True` 是默认路径：必须一个进程都不起，即使假 run 在旁边等着。"""
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    calls = []
    printed = []
    result = selfupdate.apply(cfg, prefix="C:/Python312", fetch=fake_fetch("99.0.0"),
                              run=lambda cmd: calls.append(cmd),
                              log=printed.append, dry_run=True)
    assert calls == []
    assert result["ok"] is True and result["dry_run"] is True
    assert any("[dry-run]" in line for line in printed)
    assert any("99.0.0" in line for line in printed)


def test_apply_runs_injected_runner(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    seen = {}

    def fake_run(command):
        seen["command"] = command
        return SimpleNamespace(returncode=0, stdout="Successfully installed aml-memory\n",
                               stderr="")

    result = selfupdate.apply(cfg, prefix="C:/Python312", fetch=fake_fetch("99.0.0"), run=fake_run)
    assert seen["command"] == result["command"]
    assert result["ok"] is True and result["rc"] == 0
    assert "Successfully installed" in result["output"]
    assert result["dry_run"] is False


def test_apply_reports_failure_rc(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    result = selfupdate.apply(cfg, prefix="C:/Python312", fetch=fake_fetch("99.0.0"),
                              run=lambda cmd: SimpleNamespace(returncode=2, stdout="", stderr="boom"))
    assert result["ok"] is False and result["rc"] == 2
    assert "boom" in result["output"]


def test_apply_unknown_method_does_not_run(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    calls = []
    result = selfupdate.apply(cfg, prefix="", run=lambda cmd: calls.append(cmd),
                              fetch=fake_fetch("99.0.0"))
    assert calls == [] and result["ok"] is False and result["command"] == []


def test_apply_timeout_is_reported_not_raised(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})

    def timeout_run(command):
        raise subprocess.TimeoutExpired(command, selfupdate.RUN_TIMEOUT_SEC)

    result = selfupdate.apply(cfg, prefix="C:/Python312", fetch=fake_fetch("99.0.0"),
                              run=timeout_run)
    assert result["ok"] is False and "超时" in result["output"]


def test_apply_runner_exception_is_reported_not_raised(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})

    def broken_run(command):
        raise FileNotFoundError("pipx 不在 PATH 里")

    result = selfupdate.apply(cfg, prefix="C:/pipx/venvs/aml", fetch=fake_fetch("99.0.0"),
                              run=broken_run)
    assert result["ok"] is False and "FileNotFoundError" in result["output"]


# ------------------------------------------------------------------ 渲染与元数据

def test_render_contains_key_fields(tmp_path):
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    report = selfupdate.plan(cfg, prefix="C:/Python312", fetch=fake_fetch("99.0.0"))
    text = selfupdate.render(report)
    assert "当前版本" in text and "最新版本" in text and "安装方式" in text
    assert "99.0.0" in text
    assert "pip" in text
    assert "install --upgrade aml-memory" in " ".join(text.split())


def test_render_shows_error_and_no_upgrade(tmp_path):
    """PyPI 查不到时要明说"不升级"。

    **必须显式 `source="pypi"`**：默认的 `auto` 会退到 git tag 兜底，
    而本仓库已经有 `v0.1.0` 标签 —— 兜底成功就变成"已经是最新"，断言随之失效。
    2026-09-22 实测踩到：发布 0.1.0 并打 tag 之后，这条测试从绿变红
    （它原本只是"碰巧"绿：仓库没 tag、网络又查不到）。
    """
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    report = selfupdate.plan(cfg, prefix="C:/Python312", fetch=boom_fetch(), source="pypi")
    text = selfupdate.render(report)
    assert "查不到" in text and "不升级" in text


def test_auto_falls_back_to_git_tag_when_pypi_unavailable(tmp_path):
    """PyPI 查不到 → 退到 git tag 兜底（就是打 tag 之后真实生效的那条路径）。"""
    from aml import config as cfgmod
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    calls = []

    def fake_run(command):
        calls.append(command)
        # 模仿 `git ls-remote --tags` 的真实输出（解析器只认 refs/tags/ 这一列）
        return 0, "abc123\trefs/tags/v0.1.0\ndef456\trefs/tags/v0.0.9\n"

    report = selfupdate.plan(cfg, prefix="C:/Python312", fetch=boom_fetch(),
                             run=fake_run, source="auto")
    assert report["source"] == "git" and report["latest"] == "0.1.0"
    assert calls and any("tag" in " ".join(call) for call in calls)
    assert "git tag" in selfupdate.render(report)


def test_current_version_returns_none_or_string_without_raising():
    """没装成包时必须返回 None（`PackageNotFoundError` 不能漏出去）。"""
    got = selfupdate.current_version()
    assert got is None or isinstance(got, str)


def test_index_url_survives_broken_config():
    """配置结构畸形（section 不是 dict）时不能炸 —— 查版本是只读动作。"""
    broken = SimpleNamespace(section=lambda name: "不是 dict")
    assert selfupdate.index_url(broken) == selfupdate.DEFAULT_INDEX
