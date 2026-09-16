"""包版本监控（默认是给"你在用的工具"留的钩子，例如某个 CLI）。

只监控、**不自动升级**：升级往往要重启服务/断线，还可能改变行为，
所以这里只做"有新版本 + 增加什么"，把决定权交回人。

"增加什么"两条路，都不需要 token：
  · npm registry 的 dist-tags / 版本发布时间（主依据，无配额限制）
  · 新旧版本 package.json 的依赖增减（拿不到 release notes 时的兜底事实）
  · GitHub release notes（有就给官方变更说明，匿名 API 有限流，属可选增强）
"""
from __future__ import annotations

import glob
import json
import os
import re

from . import github

STAGE_ORDER = {"alpha": 1, "beta": 2, "rc": 3, "": 4}


def semver_key(version: str) -> tuple:
    """把 0.1.5-rc.2 / 0.1.6-alpha.1 变成可比较的元组。"""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-([a-z]+)\.?(\d+)?)?", (version or "").strip())
    if not m:
        return (0, 0, 0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
            STAGE_ORDER.get(m.group(4) or "", 0), int(m.group(5) or 0))


def registry(pkg: str) -> dict:
    """取包元数据。

    关键是 `Accept: application/vnd.npm.install-v1+json`（npm 的"精简元数据"格式）：
    完整 manifest 会带上每个版本的完整 package.json，包大时是几 MB ——
    本机国际带宽实测 ~27 KB/s，取一次要 **171 秒**（踩过）。
    精简格式只有 dist-tags / 版本号 / 依赖，体积小一个数量级，够用。
    """
    quoted = pkg.replace("/", "%2F")
    data = github._get(f"https://registry.npmjs.org/{quoted}", timeout=30,
                       headers={"User-Agent": "aml-patrol",
                                "Accept": "application/vnd.npm.install-v1+json"})
    try:
        return json.loads(data)
    except ValueError:
        # 极少数情况下服务端不认精简格式：退回完整格式（慢，但不会挂）
        data = github._get(f"https://registry.npmjs.org/{quoted}", timeout=60,
                           headers={"User-Agent": "aml-patrol"})
        return json.loads(data)


def installed(pattern: str) -> dict:
    """按 glob 找本机已装的版本：{安装位置标签: 版本}。"""
    out = {}
    if not pattern:
        return out
    for path in glob.glob(os.path.expanduser(os.path.expandvars(pattern)), recursive=True):
        try:
            with open(path, encoding="utf-8") as f:
                version = json.load(f).get("version")
        except (OSError, ValueError):
            continue
        parts = os.path.normpath(path).split(os.sep)
        label = parts[-5] if len(parts) >= 5 else path    # 通常是 <缓存目录>/<安装目录名>/...
        out[label] = version
    return out


def summarize_notes(body: str, limit: int = 70) -> str:
    """从 release notes 里抠"增加什么"，压成一句话。"""
    if not body:
        return ""
    keep = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ">", "<!--", "_", "**Full Changelog")):
            continue
        line = re.sub(r"^[-*+]\s*", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"[`*]", "", line).strip()
        if not line or line.startswith(("What's Changed", "Contributors")):
            continue
        keep.append(line)
        if sum(len(x) for x in keep) > limit * 2:
            break
    text = re.sub(r"\s+", " ", "；".join(keep)).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def _manifest(pkg: str, version: str) -> dict:
    try:
        return registry(f"{pkg}/{version}").get("dependencies") or {}
    except Exception:  # noqa: BLE001
        return {}


def manifest_delta(pkg: str, old_version: str, new_version: str) -> str:
    """新旧版本 package.json 的依赖差异（拿不到 release notes 时的兜底）。"""
    if not old_version or not new_version:
        return ""
    old, new = _manifest(pkg, old_version), _manifest(pkg, new_version)
    if not old or not new:
        return ""
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    bits = []
    if added:
        bits.append(f"新增依赖 {len(added)} 个（{', '.join(added[:5])}）")
    if removed:
        bits.append(f"移除依赖 {len(removed)} 个（{', '.join(removed[:5])}）")
    if changed:
        bits.append(f"{len(changed)} 个依赖版本变化")
    return "；".join(bits)


def check(cfg, log=print, write: bool = True) -> dict:
    """检查配置里所有包。返回 {包名: {installed, target, action, summary}}。"""
    patrol = cfg.section("patrol")
    out = {}
    import time
    budget = float(patrol.get("packages_budget_sec", 60) or 60)
    started = time.time()
    for spec in patrol.get("packages") or []:
        pkg = spec.get("name")
        if not pkg:
            continue
        if time.time() - started > budget:
            log(f"  ⏱ 包监控已用 {time.time() - started:.0f}s，超预算 {budget:.0f}s，跳过剩余包（下轮再查）")
            out[pkg] = {"action": "skipped", "reason": "超预算"}
            continue
        channel = spec.get("channel", "latest")
        t0 = time.time()
        try:
            reg = registry(pkg)
        except Exception as e:  # noqa: BLE001
            log(f"  {pkg}：registry 取不到（{type(e).__name__} {str(e)[:60]}）")
            out[pkg] = {"error": f"{type(e).__name__} {str(e)[:80]}"}
            continue
        tags = reg.get("dist-tags") or {}
        target = tags.get(channel) or tags.get("latest")
        found = installed(spec.get("installed_glob") or "")
        current = max((v for v in found.values() if v), key=semver_key, default=None)
        info = {"installed": found, "current": current, "channel": channel, "target": target,
                "dist_tags": tags, "action": "none", "summary": ""}
        if not target or (current and semver_key(target) <= semver_key(current)):
            log(f"  {pkg}：已是最新（本机 {current or '未找到'}，{channel} {target}） [{time.time() - t0:.1f}s]")
            out[pkg] = info
            continue
        notes = github.release_notes(spec.get("releases_repo", ""), target,
                                     spec.get("release_tag_prefix", "")) \
            if spec.get("releases_repo") else None
        summary = summarize_notes((notes or {}).get("body", ""), 70)
        delta = manifest_delta(pkg, current, target)
        info.update({"action": "notify", "notes_url": (notes or {}).get("url", ""),
                     "summary": "；".join(x for x in (summary, delta) if x)
                     or "未取到 release notes / 依赖差异，请人工查看"})
        log(f"  {pkg}：有新版本 {target}（本机 {current}）—— {info['summary']} [{time.time() - t0:.1f}s]")
        out[pkg] = info

    if write:
        reports = cfg.state_dir / "patrol" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        import datetime as dt
        (reports / f"packages-{dt.datetime.now():%Y%m%d-%H%M%S}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
