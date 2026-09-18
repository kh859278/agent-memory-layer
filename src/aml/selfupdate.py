"""自查更新：工具查/升级自己 —— **默认只查不装**。

## 为什么默认只查不装（这条比功能重要）

`aml` 不是随手敲的一次性命令，它是**计划任务**在跑的东西（本机 09:30 的「技能DSH-巡检」）。
自升级是**不可逆动作**：

  · 升级会把 `site-packages` 里的代码换成另一份，**正在跑的那一轮巡检**可能读到半新半旧的文件；
  · 升级之后没有任何"自动回滚"——`pipx upgrade` 失败时旧版本可能已经被卸载；
  · 升级可能改变行为（配置键改名、CLI 参数变化），半夜炸掉是**没人看日志**的；
  · 而且升级通常需要联网，计划任务里最不该做的事就是"在没人看着的时候改动自己的代码"。

所以取向是：**查询可以在巡检里自动跑（只读、可失败），安装只能由人在场时显式执行**
（`apply(dry_run=False)`，CLI 那边还要人再加一道确认）。

## 三种安装方式为什么要分开

同一台机器上 `pip` / `pipx` / `uv tool` 三条路的"升级命令"完全不同，**猜错就是拿另一个环境里的包**：
`pip install --upgrade` 装到当前解释器，`pipx upgrade` 装到 pipx 的 venv，`uv tool upgrade` 装到 uv 的。
判定只看路径（`pipx` / `uv` 出现在前缀或解释器路径里），并且允许测试传 `prefix=` 覆盖——
不能在测试里去猜本机真实环境。

## 注入点

`fetch`（取远端元数据）与 `run`（执行升级命令）都是参数，默认实现才是真的联网/起进程。
测试永远用假的：**仓库里不允许有测试去碰真实网络**。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.request

PACKAGE = "agent-memory-layer"
DEFAULT_INDEX = "https://pypi.org/pypi"
# 归属标记：**PyPI 上的 `agent-memory-layer` 不是本项目**（2026-09-18 实测：那是 SAP 的
# "A reusable memory layer for SAP agentic workflows"，0.1.0/0.1.1，2026-04 上传）。
# 所以照名字升级 = 让你安装陌生人的包并把我们自己的覆盖掉。查 PyPI 前先确认包里的
# 仓库链接是我们的；不是就拒绝（这不是理论风险：本机 `aml self-update` 第一版就差点这么干）。
OWNER_MARKERS = ("kh859278/agent-memory-layer", "github.com/kh859278")
# 超时必须显式给：不给的话 urllib 会一直等到 TCP 超时（本机实测 github/PyPI 时通时断，
# 一轮巡检被一次卡死的请求拖到计划任务重叠）
TIMEOUT_SEC = 15
# 升级命令自己的上限：pip 解包 + 联网在慢网络上可能好几分钟，但绝不能无限等
RUN_TIMEOUT_SEC = 600


# ------------------------------------------------------------------ 本机状态

def current_version() -> str | None:
    """已安装的版本；**没装成包就返回 None，不抛异常**。

    为什么要容错：开发时是 `sys.path` 直连 `src/`，`importlib.metadata` 找不到分发元数据
    （`PackageNotFoundError`）。这时候应该老实说"不知道自己什么版本"，而不是让 `aml doctor` 崩掉。
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - 3.7 及更早
        return None
    try:
        return version(PACKAGE)
    except PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - 元数据目录损坏时也可能是别的异常
        return None


def _norm_path(value) -> str:
    return str(value or "").replace("\\", "/").lower()


def install_method(prefix: str | None = None, executable: str | None = None) -> str:
    """本机是怎么装的：`pipx` / `uv` / `pip` / `unknown`。

    判定顺序（uv 优先于 pipx）：`uv tool install pipx` 这种套娃是存在的——
    被管理的是 **uv 的环境**，`pipx upgrade` 在那个环境里根本不对。
    判据只用路径，不看包元数据：元数据只能告诉你"有这个包"，看不出它住在哪条工具链里。
    """
    if prefix is not None or executable is not None:
        # 显式给了路径 = 调用方（测试）指定了环境，**不要**再混进本机真实前缀去猜
        probe = [prefix, executable]
    else:
        probe = [sys.prefix, sys.executable]
    if not any(probe):
        return "unknown"          # 什么都识别不出来（空环境变量 / 测试传空值）
    if any(raw and "uv" in _norm_path(raw) for raw in probe):
        return "uv"
    if any(raw and "pipx" in _norm_path(raw) for raw in probe):
        return "pipx"
    return "pip"


# ------------------------------------------------------------------ 查远端

def index_url(cfg=None) -> str:
    """索引地址：配置 `patrol.selfupdate.index_url` 可覆盖（内网镜像/离线源）。"""
    default = DEFAULT_INDEX
    base = getattr(cfg, "section", None)
    if callable(base):
        try:
            section = cfg.section("patrol") or {}
            section = section.get("selfupdate") or {}
            configured = section.get("index_url")
        except Exception:  # noqa: BLE001 - 配置畸形不配让"查版本"这个只读动作失败
            configured = None
        if configured:
            return str(configured).rstrip("/")
    return default


def default_fetch(url: str, timeout: int = TIMEOUT_SEC) -> dict:
    """默认取数实现：标准库 urllib，**失败只抛异常**（由 `latest_version` 兜住）。"""
    request = urllib.request.Request(url, headers={"User-Agent": "aml-selfupdate",
                                                   "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 只读 JSON
        payload = response.read()
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    import json
    return json.loads(payload)


def latest_version(cfg=None, fetch=None, verify_owner: bool = True) -> dict:
    """查索引上的最新版：`{"version", "url", "error"}`。

    失败**不抛异常**，把原因写进 `error`：这个函数是给计划任务用的，
    网络抖一下就把整轮巡检打断，等于"因为查更新失败所以连技能也不更新了"。
    """
    fetcher = fetch or default_fetch
    url = f"{index_url(cfg)}/{PACKAGE}/json"
    try:
        data = fetcher(url) or {}
    except Exception as e:  # noqa: BLE001 - 网络/解析/权限，一律降级成 error 字符串
        return {"version": None, "url": url, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    info = data.get("info") if isinstance(data, dict) else None
    version = (info or {}).get("version")
    if not version:
        return {"version": None, "url": url, "error": "索引里没有 info.version（返回的不是 PyPI JSON？）"}
    if verify_owner:
        owner = ownership(info)
        if not owner["ours"]:
            return {"version": None, "url": url, "error": owner["why"], "foreign": True,
                    "foreign_version": str(version), "summary": owner["summary"]}
    return {"version": str(version), "url": url, "error": None}


def ownership(info: dict) -> dict:
    """索引上这个包是不是我们的：看 project_urls / home_page 里有没有我们的仓库标记。"""
    info = info or {}
    urls = " ".join(str(v) for v in (info.get("project_urls") or {}).values())
    text = f"{urls} {info.get('home_page') or ''}".lower()
    if any(marker.lower() in text for marker in OWNER_MARKERS):
        return {"ours": True, "why": None, "summary": info.get("summary") or ""}
    summary = str(info.get("summary") or "").strip()
    return {
        "ours": False,
        "summary": summary,
        "why": (f"索引上的 `{PACKAGE}` **不是本项目**"
                + (f"（它是：{summary[:70]}）" if summary else "（缺 project_urls，无法确认归属）")
                + "；本项目还没发布到 PyPI —— 不要用这个名字升级，否则会装成别人的包。"
                  "改用 git 源查上游 tag（`aml self-update --source git`）"),
    }


def remote_repo(cfg=None) -> str:
    """自查更新该盯哪个仓库：配置 `patrol.selfupdate.repo` 可覆盖。"""
    default = f"https://github.com/{OWNER_MARKERS[0]}.git"
    if cfg is not None and callable(getattr(cfg, "section", None)):
        try:
            configured = ((cfg.section("patrol") or {}).get("selfupdate") or {}).get("repo")
        except Exception:  # noqa: BLE001 - 配置畸形不该让只读查询失败
            configured = None
        if configured:
            return str(configured)
    return default


def git_latest(cfg=None, run=None) -> dict:
    """从**我们自己的仓库**取最新 tag（PyPI 改名/发布之前，这才是可用的升级源）。

    用 `git ls-remote --tags`：不吃 GitHub API 限流、不需要 token。
    解析不出任何 tag 就返回 error —— 不拿分支 HEAD 冒充版本号。
    """
    repo = remote_repo(cfg)
    command = ["git", "ls-remote", "--tags", repo]
    try:
        if run is not None:
            rc, out = run(command)
        else:
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
            proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=TIMEOUT_SEC, env=env)
            rc, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:  # noqa: BLE001 - 网络/凭证一律降级
        return {"version": None, "url": repo, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    if rc != 0:
        return {"version": None, "url": repo, "error": f"git ls-remote 失败：{out.strip()[-120:]}"}
    versions = []
    for line in (out or "").splitlines():
        if "refs/tags/" not in line:
            continue
        ref = line.split("refs/tags/")[-1].strip()
        if ref.endswith("^{}"):        # 附注 tag 的 peel 行，去重
            ref = ref[:-3]
        if ref:
            versions.append(ref.lstrip("v"))
    if not versions:
        return {"version": None, "url": repo, "error": f"{repo} 还没有 tag（没有可比较的版本）"}
    best = versions[0]
    for item in versions[1:]:
        if _compare_keys(version_key(item), version_key(best)) > 0:
            best = item
    return {"version": best, "url": repo, "error": None}


def resolve_latest(cfg=None, fetch=None, run=None, source: str = "auto") -> dict:
    """最新版从哪来：`auto` = 先查 PyPI（**校验归属**），不是我们的就退到 git tag。"""
    if source not in ("pypi", "git"):
        source = "auto"
    if source in ("pypi", "auto"):
        remote = latest_version(cfg, fetch=fetch, verify_owner=True)
        if remote.get("version"):
            return {**remote, "source": "pypi"}
        if source == "pypi":
            return {**remote, "source": "pypi"}
        git_remote = git_latest(cfg, run=run)
        note = remote.get("error")
        if git_remote.get("version"):
            return {**git_remote, "source": "git", "note": note}
        return {**git_remote, "source": "git",
                "error": git_remote.get("error") or note, "note": note,
                "foreign": remote.get("foreign")}
    remote = git_latest(cfg, run=run)
    return {**remote, "source": "git"}


def version_key(value: str) -> tuple:
    """把版本串压成可比较的数字元组，**必须容错**。

    为什么不用 `packaging.version.parse`：那会给项目加一个依赖，而且 PyPI 之外的索引
    可能返回 `v1.2`、`0.1.0+local`、`0.1.0rc1` 这类东西。这里只抠数字：
    `0.1.0rc1` → `(0, 1, 0, 1)`、`v1.2.3` → `(1, 2, 3)`；一个数字都没有就返回 `()`（视为最旧）。
    """
    # 先砍掉构建元数据（`0.1.0+local` → `0.1.0`）：`+` 后面是本地版本标识，
    # 不是新版本，参与比较会把本机自建版本误报成"比发布版新"
    text = str(value or "").split("+", 1)[0]
    return tuple(int(x) for x in re.findall(r"\d+", text))


def _compare_keys(left: tuple, right: tuple) -> int:
    """按**深度**比较两个版本键，返回 -1 / 0 / 1。

    为什么要写这个而不是直接用元组比较（而且第 4 位是预发布标记）：
    `0.1.0rc1` 由上面抠成 `(0, 1, 0, 1)`，而 `0.1.0` 只有 `(0, 1, 0)`，
    元组的默认规则会判定 `(0,1,0,1) > (0,1,0)` —— 等于把**预发布版报成"有新版本"**，
    方向刚好反了（升级到 rc 是降级）。所以主版本段按位置比，位数更多时再看第 4 位：
    有第 4 位（预发布标记）的更旧，没有的（正式版）更新。
    已知取舍：`1.0.0.1`（四段正式版）会被当成 `1.0.0` 的预发布 —— 这个仓库的版本号是
    `X.Y.Z` 三段，出现四段是事故，宁可保守（不报升级）也不误报。
    """
    if left == right:
        return 0
    for index in range(min(len(left), len(right))):
        if left[index] != right[index]:
            return -1 if left[index] < right[index] else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) > len(right) else 1


def needs_upgrade(current: str | None, latest: str | None) -> bool:
    """要不要升级：只认"远端更高"（`0.1.0` → `0.1.0rc1` **不算**升级）。

    "本机没有版本信息"单列一条：`version_key(None)` 是空元组，直接进 `_compare_keys`
    会当成"最旧"从而永远报升级 —— 而"没装成包"（开发态 `sys.path` 直连 `src/`）
    确实该提示"远端有个版本"，但那是**安装**不是升级，所以这里只说 `needs_upgrade`，
    `apply` 仍然要人显式跑（本模块默认从不自己动手）。
    """
    if not latest:
        return False
    if not current:
        return True
    return _compare_keys(version_key(current), version_key(latest)) < 0


def command_for(method: str, prefix: str | None = None) -> list:
    """建议的升级命令（argv 列表，不拼 shell 字符串：拼接会踩空格与引号的坑）。"""
    if method == "pipx":
        return ["pipx", "upgrade", PACKAGE]
    if method == "uv":
        return ["uv", "tool", "upgrade", PACKAGE]
    if method == "unknown":
        return []
    argv = [sys.executable, "-m", "pip"]
    if _user_install_needed(prefix):
        argv.append("--user")
    return argv + ["install", "--upgrade", PACKAGE]


def _user_install_needed(prefix: str | None) -> bool:
    """裸 `pip install`（没在 venv、环境又是系统 Python）要加 `--user`。

    不加的后果不是报错而是**更糟**：Linux 上直接 `Permission denied`，
    Windows 上装进 `Program Files` 需要管理员。加 `--user` 谁都能跑。
    venv 里则**不能**加：`--user` 在 venv 中会装到用户目录、绕开 venv（pip 只给个警告）。
    """
    if prefix is not None:
        # 调用方显式给了前缀：按"这是系统/用户级安装"处理，保持行为可预测
        explicit = True
    else:
        try:
            explicit = _norm_path(sys.prefix) == _norm_path(getattr(sys, "base_prefix", ""))
        except Exception:  # noqa: BLE001
            explicit = False
    return explicit


def plan(cfg=None, prefix=None, fetch=None, run=None, source: str = "auto") -> dict:
    """完整计划：当前/最新（含来源）/安装方式/建议命令/要不要升/错误。

    注意 `error` 只有"查不到"才算；**查不到不等于不需要升级**，`needs_upgrade` 保持 False。
    `source`：`auto`（PyPI 优先，归属校验不过就退 git tag）/ `pypi` / `git`。
    """
    current = current_version()
    method = install_method(prefix)
    remote = resolve_latest(cfg, fetch=fetch, run=run, source=source)
    latest = remote.get("version")
    return {
        "current": current,
        "latest": latest,
        "method": method,
        "command": command_for(method, prefix),
        "needs_upgrade": needs_upgrade(current, latest),
        "error": remote.get("error"),
        "index_url": remote.get("url"),
        "source": remote.get("source"),
        "foreign": bool(remote.get("foreign")),
        "note": remote.get("note"),
    }


# ------------------------------------------------------------------ 装（默认不动手）

def default_run(command: list):
    """默认执行器：capture_output + text，超时 600s。

    为什么 capture 而不是让它直接打到终端：升级输出要能原样写进报告/通知里
    （巡检看不到终端）。超时抛 `TimeoutExpired` 由调用方兜。
    """
    return subprocess.run(command, capture_output=True, text=True, timeout=RUN_TIMEOUT_SEC)


def _as_text(value) -> str:
    """stdout/stderr 归一成 str：注入的假 run 可能塞 None 或 bytes。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _output_of(result, limit: int = 2000) -> str:
    out = _as_text(getattr(result, "stdout", ""))
    err = _as_text(getattr(result, "stderr", ""))
    text = "\n".join(x for x in (out.strip(), err.strip()) if x)
    return text[:limit]


def apply(cfg=None, prefix=None, run=None, log=print, dry_run=False, fetch=None) -> dict:
    """执行升级。**`dry_run=True` 只打印要执行什么，一个进程都不起**。

    这是本模块的默认路径：自升级不可逆，计划任务里永远不会走到真正的执行分支。

    `fetch` 只为了让调用方（测试）把"查最新版"也换成假的：
    `apply` 内部要调 `plan`，而 `plan` 默认会联网 —— 不开口子的话
    "dry_run 不执行"这条测试本身就得先碰一次真实网络。
    """
    info = plan(cfg, prefix=prefix, fetch=fetch)
    command = info["command"]
    if dry_run:
        if command:
            log(f"  [dry-run] 将执行：{' '.join(command)}"
                f"（当前 {info['current'] or '未知'} → 最新 {info['latest'] or '未知'}）")
        else:
            log("  [dry-run] 没识别出安装方式，不知道该怎么升级（人工确认后再动手）")
        return {"ok": True, "rc": 0, "output": "", "command": command, "dry_run": True,
                "needs_upgrade": info["needs_upgrade"], "error": info["error"]}
    if not command:
        return {"ok": False, "rc": 1, "output": "没识别出安装方式（pip/pipx/uv 都不是）",
                "command": [], "dry_run": False, "needs_upgrade": info["needs_upgrade"],
                "error": info["error"]}
    runner = run or default_run
    try:
        result = runner(command)
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": 1, "output": f"升级超时（>{RUN_TIMEOUT_SEC}s），已放弃",
                "command": command, "dry_run": False, "needs_upgrade": info["needs_upgrade"],
                "error": info["error"]}
    except Exception as e:  # noqa: BLE001 - 命令不存在/权限不足，一律降级成结构化失败
        return {"ok": False, "rc": 1, "output": f"{type(e).__name__}: {str(e)[:200]}",
                "command": command, "dry_run": False, "needs_upgrade": info["needs_upgrade"],
                "error": info["error"]}
    rc = int(getattr(result, "returncode", 1) or 0)
    return {"ok": rc == 0, "rc": rc, "output": _output_of(result), "command": command,
            "dry_run": False, "needs_upgrade": info["needs_upgrade"], "error": info["error"]}


# ------------------------------------------------------------------ 人看的输出

def render(report: dict) -> str:
    """一行行给人看：当前/最新（含来源）/安装方式/建议命令/错误。"""
    source = {"pypi": "PyPI", "git": "git tag"}.get(report.get("source") or "", report.get("source"))
    lines = [
        f"当前版本：{report.get('current') or '未知（没装成包，或元数据缺失）'}",
        f"最新版本：{report.get('latest') or '未知'}"
        + (f"（来源：{source}）" if source and report.get("latest") else "")
        + (f"（查不到：{report['error']}）" if report.get("error") else ""),
        f"安装方式：{report.get('method') or 'unknown'}",
    ]
    if report.get("note"):
        lines.append(f"注意：{report['note']}")
    if report.get("needs_upgrade"):
        command = report.get("command") or []
        lines.append("有新版本，建议命令（由你决定要不要跑）：")
        lines.append("  " + (" ".join(command) if command else "（没识别出安装方式，无法给命令）"))
    elif report.get("error"):
        lines.append("查不到最新版——不升级（查不到 ≠ 需要升级）")
    else:
        lines.append("已经是最新（或本地版本不低），不需要动")
    if report.get("dry_run"):
        lines.append("（dry-run：只打印，没执行任何命令）")
    if "rc" in report and not report.get("dry_run"):
        lines.append(f"执行结果：rc={report.get('rc')} {'成功' if report.get('ok') else '失败'}")
        for line in (report.get("output") or "").splitlines()[-10:]:
            lines.append(f"  | {line}")
    return "\n".join(lines)


__all__ = ["PACKAGE", "DEFAULT_INDEX", "TIMEOUT_SEC", "RUN_TIMEOUT_SEC", "OWNER_MARKERS",
           "current_version", "install_method", "index_url", "default_fetch", "latest_version",
           "ownership", "remote_repo", "git_latest", "resolve_latest", "version_key",
           "needs_upgrade", "command_for", "plan", "default_run", "apply", "render"]
