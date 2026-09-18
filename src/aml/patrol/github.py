"""上游探测：拿"远端到底有没有更新"，并尽量便宜地拿。

实战结论（这几种路子的可靠性和代价差得很远）：
  · `git ls-remote`：~2 秒/仓库，**不吃 GitHub API 限流**。
    本机 git 默认走 schannel，在受限环境里会报
    `schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS`
    （不是网络问题，是拿不到系统凭证）——加 `-c http.sslBackend=openssl` 即可。
  · GitHub API `/commits/{branch}`：稳定但有匿名 60 次/小时限流，几轮调试就吃光。
  · `codeload` tarball：**不走 api.github.com，不吃限流**，适合离线比内容；
    代价是有的仓库有几 MB（本机实测 27 KB/s，全量下能把一轮巡检拖到十几分钟）。

所以顺序是：先便宜地拿 sha（git → API），只有"确实有变化"或"拿不到 sha"时才下内容。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import time
import urllib.request
import uuid

UA = {"User-Agent": "aml-patrol", "Accept": "application/vnd.github+json"}

# 显式白名单：以前"只信 github.com"是隐含在代码里的（URL 都是拼出来的），
# 现在把它变成**可配置 + 可审计**的一条规则（用户点名的第 5 项）。
DEFAULT_HOSTS = ("github.com", "codeload.github.com", "api.github.com",
                 "raw.githubusercontent.com")


def allowed_hosts(cfg=None) -> tuple:
    hosts = None
    if cfg is not None:
        hosts = cfg.section("patrol").get("allowed_hosts")
    return tuple(hosts) if hosts else DEFAULT_HOSTS


def check_url(url: str, cfg=None) -> str:
    """主机白名单校验：不在白名单里直接拒绝（并说清怎么加）。"""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    hosts = allowed_hosts(cfg)
    if host and host not in hosts:
        raise PermissionError(
            f"拒绝访问 {host}：不在 patrol.allowed_hosts 白名单里"
            f"（现有：{', '.join(hosts)}）。确认可信就把它写进配置再试。")
    return host


def check_repo(repo: str, cfg=None) -> str:
    """`owner/name` 形式的仓库标识：仓名本身不接受主机，天然只在 github.com 下。"""
    if "/" not in str(repo) or str(repo).startswith(("http://", "https://", "git@")):
        raise ValueError(f"来源要写成 owner/repo（不是 URL）：{repo!r}")
    return str(repo)


def _get(url: str, timeout: int = 30, headers: dict | None = None, cfg=None):
    check_url(url, cfg)
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return f.read()


def get_bytes(url: str, deadline: float = 20, headers: dict | None = None, timeout: int = 15,
              cfg=None):
    """带**墙钟截止**的下载。

    为什么不能只靠 urllib 的 timeout：它是"单次 socket 操作"的超时，
    遇到"缓慢滴流"的连接（每几秒来一点字节）永远不触发 ——
    实测 npm registry 131 KB 拖了 181 秒。这里用工作线程 + join(deadline) 硬截止。
    """
    import threading
    check_url(url, cfg)
    box = {}

    def work():
        try:
            box["data"] = _get(url, timeout=timeout, headers=headers, cfg=cfg)
        except Exception as e:  # noqa: BLE001
            box["error"] = e

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        raise TimeoutError(f"超过 {deadline:.0f}s 未完成（连接被拖住，已放弃）")
    if "error" in box:
        raise box["error"]
    return box["data"]


def api_json(url: str, timeout: int = 30, deadline: float = 20, cfg=None) -> dict:
    """GitHub API：同样要墙钟截止。

    踩过的坑：`timeout` 只管单次 socket 操作，遇到"缓慢滴流"的连接（每几秒来一点字节）
    永远不触发——实测单个仓库的 API 探测拖了 301 秒，把整轮预算都吃光了。
    """
    return json.loads(get_bytes(url, deadline=deadline, timeout=timeout, headers=UA, cfg=cfg))


# --------------------------------------------------------------------- sha

def ls_remote(repo: str, branch: str | None = None, timeout: int = 15, tries: int = 1) -> str:
    """git ls-remote 取远端 sha。

    超时默认 15 秒、只试 1 次：本机 github 时通时断，**重试一个"挂住"的连接没有意义**，
    只会把每仓库代价从 15s 变成 50s（5 个仓库 = 4 分钟以上，实测踩过）。
    失败就让调用方退到 GitHub API（另一条传输路径，常常反而不挂）。凭证类错误也不重试。
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
    check_repo(repo)
    ref = f"refs/heads/{branch}" if branch else "HEAD"
    cmd = ["git", "-c", "http.sslBackend=openssl", "ls-remote",
           f"https://github.com/{repo}.git", ref]
    last = ""
    for attempt in range(max(1, tries)):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
            out = (r.stdout or "").strip()
            if r.returncode == 0 and out:
                return out.split()[0]
            last = (r.stderr or "ls-remote 无输出").strip().splitlines()[-1][:120]
            if "schannel" in last or "CREDENTIALS" in last.upper():
                break
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__} {str(e)[:80]}"
        if attempt + 1 < tries:
            time.sleep(2)
    raise RuntimeError(last)


def remote_sha(repo: str, branch: str | None = None, git_timeout: int = 15) -> tuple:
    """返回 (sha, 途径)。先 git，失败退 API（另一条传输路径）。"""
    try:
        return ls_remote(repo, branch, timeout=git_timeout), "git"
    except Exception as git_error:  # noqa: BLE001
        try:
            url = (f"https://api.github.com/repos/{repo}/commits/{branch}" if branch
                   else f"https://api.github.com/repos/{repo}/commits")
            return api_json(url)["sha"], "api"
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"两条路都失败：{git_error}") from git_error


def default_branch(repo: str, cfg=None) -> str:
    return api_json(f"https://api.github.com/repos/{repo}", cfg=cfg)["default_branch"]


# ---------------------------------------------------------------- tarball

def scratch_dir(cfg, prefix: str = "repo-") -> str:
    """解包用的临时目录。

    放在**数据目录**下而不是系统临时目录：一是沙箱环境常常不让写系统 temp，
    二是有些平台建临时目录用 0700，受限环境会直接拒绝访问（都踩过）。
    """
    base = cfg.home / "_tmp"
    base.mkdir(parents=True, exist_ok=True)
    path = os.path.join(str(base), prefix + uuid.uuid4().hex[:8])
    os.makedirs(path, exist_ok=True)
    return path


def _extract(tf: tarfile.TarFile, dest: str) -> None:
    """手动解包：不用 extractall（它用 os.mkdir(path, 0o700)，受限环境会被拒），
    同时避免路径穿越与符号链接。"""
    os.makedirs(dest, exist_ok=True)
    for member in tf.getmembers():
        if member.isdir():
            os.makedirs(os.path.join(dest, member.name), exist_ok=True)
        elif member.isfile():
            target = os.path.join(dest, member.name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tf.extractfile(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def _codeload(repo: str, branch: str, dest: str, max_mb: int = 40, cfg=None) -> str:
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}"
    data = _get(url, timeout=180, headers={"User-Agent": UA["User-Agent"]}, cfg=cfg)
    if max_mb and len(data) > max_mb * 1024 * 1024:
        raise RuntimeError(f"快照太大（{len(data)/1024/1024:.1f} MB > {max_mb} MB 上限），跳过")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        names = tf.getnames()
        base = names[0].split("/")[0] if names else ""
        _extract(tf, dest)
    root = os.path.join(dest, base)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"解包后找不到根目录：{dest}")
    return root


# --------------------------------------------------------------------- 缓存

def cache_path(cfg, repo: str, branch: str) -> str:
    """tarball 缓存位置：`state/patrol/_cache/<owner>__<repo>/<branch>.tar.gz`。"""
    safe = repo.replace("/", "__")
    return os.path.join(str(cfg.state_dir / "patrol" / "_cache" / safe), f"{branch}.tar.gz")


def cache_info(cfg, repo: str, branch: str) -> dict:
    path = cache_path(cfg, repo, branch)
    ttl = float(cfg.section("patrol").get("cache_ttl_sec", 900) or 0)
    if not os.path.isfile(path):
        return {"path": path, "hit": False, "age": None, "ttl": ttl}
    age = time.time() - os.path.getmtime(path)
    return {"path": path, "hit": ttl > 0 and age <= ttl, "age": round(age, 1), "ttl": ttl,
            "size": os.path.getsize(path)}


def _download_tarball(cfg, repo: str, branch: str, max_mb: int) -> str:
    """下载并**写进缓存**（先写临时文件再原子替换，避免半截文件被当成缓存命中）。"""
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}"
    data = _get(url, timeout=180, headers={"User-Agent": UA["User-Agent"]}, cfg=cfg)
    if max_mb and len(data) > max_mb * 1024 * 1024:
        raise RuntimeError(f"快照太大（{len(data)/1024/1024:.1f} MB > {max_mb} MB 上限），跳过")
    path = cache_path(cfg, repo, branch)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path


def _extract_cached(tar_path: str, dest: str) -> str:
    with tarfile.open(tar_path, mode="r:gz") as tf:
        names = tf.getnames()
        base = names[0].split("/")[0] if names else ""
        _extract(tf, dest)
    root = os.path.join(dest, base)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"解包后找不到根目录：{dest}")
    return root


def fetch_repo(cfg, repo: str, branch: str | None = None, branches=("main", "master"),
               force_refresh: bool = False) -> tuple:
    """取回仓库快照。返回 (解包根目录, 实际分支, 临时目录)。

    **缓存**：tarball 落在 `state/patrol/_cache/`，TTL 内不重复下载（`--force-refresh` 跳过）。
    解包永远解到新的临时目录，所以调用方照旧 `rmtree(scratch)` 不会把缓存删掉。
    代价从"每轮每个仓库都下几 MB"降到"TTL 内零下载"——定时任务最需要的就是这个。
    """
    check_repo(repo, cfg)
    dest = scratch_dir(cfg)
    tried = []
    order = ([branch] if branch else []) + [b for b in branches if b != branch]
    max_mb = int(cfg.section("patrol").get("update", {}).get("tarball_max_mb", 40))
    for candidate in order:
        try:
            info = cache_info(cfg, repo, candidate)
            tar_path = info["path"]
            if info["hit"] and not force_refresh:
                return _extract_cached(tar_path, dest), candidate, dest
            tar_path = _download_tarball(cfg, repo, candidate, max_mb)
            return _extract_cached(tar_path, dest), candidate, dest
        except Exception as e:  # noqa: BLE001
            tried.append(f"{candidate}: {type(e).__name__} {str(e)[:50]}")
            for name in os.listdir(dest):
                shutil.rmtree(os.path.join(dest, name), ignore_errors=True)
    shutil.rmtree(dest, ignore_errors=True)
    raise RuntimeError(f"{repo} 取不到快照（试过 {'；'.join(tried)}）")


def prune_cache(cfg, keep_days: int = 14) -> int:
    """清掉太久没用的 tarball 缓存，返回删了几个文件。"""
    root = cfg.state_dir / "patrol" / "_cache"
    if not root.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for base, _dirs, files in os.walk(str(root)):
        for name in files:
            path = os.path.join(base, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    return removed


def release_notes(repo: str, version: str, tag_prefix: str = "", cfg=None) -> dict | None:
    """拿官方 release notes（优先带前缀的 tag）。拿不到返回 None。"""
    for tag in (f"{tag_prefix}{version}", f"v{version}", version):
        try:
            data = api_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", cfg=cfg)
            return {"tag": data.get("tag_name"), "url": data.get("html_url"),
                    "published": (data.get("published_at") or "")[:10], "body": data.get("body") or ""}
        except Exception:  # noqa: BLE001
            continue
    return None
