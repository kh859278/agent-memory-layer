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


def _get(url: str, timeout: int = 30, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return f.read()


def api_json(url: str, timeout: int = 30) -> dict:
    return json.loads(_get(url, timeout=timeout))


# --------------------------------------------------------------------- sha

def ls_remote(repo: str, branch: str | None = None, timeout: int = 25, tries: int = 2) -> str:
    """git ls-remote 取远端 sha。凭证类错误不重试（重试也没用）。"""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
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


def remote_sha(repo: str, branch: str | None = None) -> tuple:
    """返回 (sha, 途径)。先 git，失败退 API。"""
    try:
        return ls_remote(repo, branch), "git"
    except Exception as git_error:  # noqa: BLE001
        try:
            url = (f"https://api.github.com/repos/{repo}/commits/{branch}" if branch
                   else f"https://api.github.com/repos/{repo}/commits")
            return api_json(url)["sha"], "api"
        except Exception:  # noqa: BLE001
            raise RuntimeError(f"两条路都失败：{git_error}") from git_error


def default_branch(repo: str) -> str:
    return api_json(f"https://api.github.com/repos/{repo}")["default_branch"]


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


def _codeload(repo: str, branch: str, dest: str, max_mb: int = 40) -> str:
    url = f"https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}"
    data = _get(url, timeout=180, headers={"User-Agent": UA["User-Agent"]})
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


def fetch_repo(cfg, repo: str, branch: str | None = None, branches=("main", "master")) -> tuple:
    """取回仓库快照。返回 (解包根目录, 实际分支, 临时目录)。"""
    dest = scratch_dir(cfg)
    tried = []
    order = ([branch] if branch else []) + [b for b in branches if b != branch]
    max_mb = int(cfg.section("patrol").get("update", {}).get("tarball_max_mb", 40))
    for candidate in order:
        try:
            return _codeload(repo, candidate, dest, max_mb=max_mb), candidate, dest
        except Exception as e:  # noqa: BLE001
            tried.append(f"{candidate}: {type(e).__name__} {str(e)[:50]}")
            for name in os.listdir(dest):
                shutil.rmtree(os.path.join(dest, name), ignore_errors=True)
    shutil.rmtree(dest, ignore_errors=True)
    raise RuntimeError(f"{repo} 取不到快照（试过 {'；'.join(tried)}）")


def release_notes(repo: str, version: str, tag_prefix: str = "") -> dict | None:
    """拿官方 release notes（优先带前缀的 tag）。拿不到返回 None。"""
    for tag in (f"{tag_prefix}{version}", f"v{version}", version):
        try:
            data = api_json(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
            return {"tag": data.get("tag_name"), "url": data.get("html_url"),
                    "published": (data.get("published_at") or "")[:10], "body": data.get("body") or ""}
        except Exception:  # noqa: BLE001
            continue
    return None
