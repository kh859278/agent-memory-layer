"""技能差异审查：`accept` 之前先看清"上游到底改了什么"。

为什么需要它：`patrol check` 只告诉你"上游 commit 变了"，`_pending/` 里只有一份新内容。
但**"变了"不是可决策的信息**——你要知道的是：

  · 正文哪里改了（不是整篇替换）
  · **新增了什么能力**：网络调用？shell？读密钥？写文件？git 写操作？
  · 这些能力是新增的，还是本来就有（本地版也有 → 不算新增风险）

这就是外部 review 说的关键一步：把"更新治理"升级成"治理"。
默认**不联网**：优先审已经暂存的 `_pending/`（那正是你准备 accept 的东西）。

退出码：0 正常；加 `--fail-on-risk` 时，若出现**新增高风险信号**返回 2（可用于门禁/CI）。
"""
from __future__ import annotations

import os
import re

from . import github, skills, update

# 高风险 = 新版本里多出来的这些，意味着"照做会改变系统状态"
HIGH_RISK = ("network", "shell", "secrets", "filesystem-write", "git-write")

# 误报是被实测打回来的：第一次拿真技能跑 `patrol diff --fetch`，Markdown 的引用块 `> xxx`
# 被判成"写文件"、行内反引号被判成"shell"——这种狼来了的信号等于没有信号。
# 所以规则只认**可执行的调用形态**，不认文档排版符号：
#   · shell：要求代码围栏语言（```bash）或显式调用（bash -c / subprocess.run / os.system）
#   · 写文件：要求真的删除/写入命令（rm -rf / Remove-Item / shutil.rmtree / Set-Content），
#            不认裸的重定向符 `>`
#   · 网络：要求发起请求的动词；**裸链接**另算一类（只提到链接不等于能力）
NETWORK_VERB = re.compile(r"\bcurl\s|\bwget\s|Invoke-WebRequest|Invoke-RestMethod|"
                          r"\brequests\.(get|post|put|delete|patch)|urlopen\s*\(|"
                          r"\bfetch\s*\(|httpx\.(get|post)", re.I)
URL_RE = re.compile(r"https?://[^\s)\]\"'`]+", re.I)

RISK_PATTERNS = [
    ("network", "发起网络请求", NETWORK_VERB),
    ("shell", "执行 shell/命令",
     re.compile(r"```(?:bash|sh|zsh|shell|powershell|pwsh|cmd|console)\b|"
                r"\b(bash|sh|zsh|pwsh|powershell)\s+-[a-zA-Z]|cmd\.exe\s*/c|"
                r"\bsubprocess\.(run|Popen|call|check_call|check_output)|"
                r"\bos\.system|Invoke-Expression", re.I)),
    ("secrets", "读凭据/密钥/环境变量",
     re.compile(r"\.ssh|id_rsa|id_ed25519|\.env\b|\.credentials|credential|api[_-]?key|"
                r"secret|password|GITHUB_TOKEN|\.netrc", re.I)),
    ("filesystem-write", "写文件/删改",
     re.compile(r"\brm\s+-[rf]|\bdel\s+/|\bRemove-Item\b|\bshutil\.(rmtree|move|copy)|"
                r"\bos\.(remove|unlink|rename)|\bmv\s+\S+\s+\S+|\btruncate\s|"
                r"\bSet-Content\b|\bOut-File\b|\btee\s+-a", re.I)),
    ("git-write", "git 写操作",
     re.compile(r"git\s+(push|commit|reset\s+--hard|clean\s+-[fd]|checkout\s+--|branch\s+-D|"
                r"rebase|filter-branch)", re.I)),
    ("install", "安装依赖/软件",
     re.compile(r"\b(npm|pnpm|yarn|pip|pipx|uv|brew|apt|apt-get|choco|winget|go)\s+"
                r"(i|install|add|get)\b", re.I)),
    ("browser", "浏览器自动化",
     re.compile(r"playwright|puppeteer|selenium|chrome-devtools|DevTools Protocol", re.I)),
    # 只提到链接不等于能力（文档里到处都是）——单列一类，算"留意"不算高风险
    ("url", "提到外部链接",
     re.compile(r"https?://[^\s)\]\"'`]+", re.I)),
]

TEXT_EXTS = (".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".ps1", ".js", ".ts", "")


def scan_signals(text: str) -> dict:
    """扫出一段文本里的"能力信号"：{类别: [命中片段...]}。"""
    text = text or ""
    found: dict = {}
    for category, _label, pattern in RISK_PATTERNS:
        hits = {m.group(0).strip()[:60] for m in pattern.finditer(text) if m.group(0).strip()}
        if hits:
            found[category] = hits
    # 只报 "curl" 没有意义，要报 "curl 到哪个地址"：把发起请求那一行里的 URL 归进网络信号
    for line in text.splitlines():
        if NETWORK_VERB.search(line):
            for url in URL_RE.findall(line):
                found.setdefault("network", set()).add(url[:60])
    return {k: sorted(v)[:8] for k, v in found.items()}


def dir_signals(root: str) -> dict:
    """扫一个技能目录里所有文本文件的能力信号（合并同类）。"""
    merged = {}
    for rel, path in skills.skill_files(root).items():
        if os.path.splitext(rel)[1].lower() not in TEXT_EXTS:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for category, hits in scan_signals(text).items():
            bucket = merged.setdefault(category, set())
            bucket.update(hits)
    return {k: sorted(v)[:12] for k, v in merged.items()}


# ---------------------------------------------------------------- 证据分级（2026-09-21）
#
# `scan_signals` 只知道"这段文字里出现了 shell 形态"，**分不清是叫你执行还是举了个例子**。
# 实测代价：第一版把 Markdown 引用块 `>` 当写文件、行内反引号当 shell，全是误报 ——
# "狼来了"的信号等于没有信号。所以治理用的判据必须是**带语境的证据等级**：
#
#   declared       skill.yaml 里显式声明（最可信，来自人）
#   instructional  文本在**要求 agent 执行**（"运行/执行/run the following…"，或紧跟在祈使句后的代码块）
#   example        明确是示例（"例如/示例/Example/e.g." 之后的代码块或行）
#   mention        只是提到了（文档里的裸链接、叙述性提及）
#
# 闸门只认 declared / instructional；example 与 mention 只提示、不拦截。
EXAMPLE_MARKER = re.compile(r"例如|示例|例子|举个|比如|Example|e\.g\.|for example|illustration",
                            re.I)
INSTRUCTION_VERB = re.compile(
    r"运行|执行|跑(?:一下|一次|这个|起来)?|调用|安装|下载|上传|删除|写入|写到|请用|使用|"
    r"\b(?:run|execute|invoke|install|download|upload|delete|write|call|use)\b", re.I)
FENCE_RE = re.compile(r"^\s*(```+|~~~+)\s*([A-Za-z0-9_+.\-]*)")
EVIDENCE_LEVELS = ("mention", "example", "instructional", "declared")
EVIDENCE_RANK = {name: index for index, name in enumerate(EVIDENCE_LEVELS)}
EVIDENCE_WINDOW = 3          # 祈使句/示例标记往前看几行（多行指令很常见）


def _line_hits(line: str) -> dict:
    found = {}
    for category, _label, pattern in RISK_PATTERNS:
        got = {m.group(0).strip()[:60] for m in pattern.finditer(line) if m.group(0).strip()}
        if got:
            found[category] = got
    if NETWORK_VERB.search(line):
        for url in URL_RE.findall(line):
            found.setdefault("network", set()).add(url[:60])
    return found


def _record(out: dict, found: dict, level: str) -> None:
    for category, hits in found.items():
        out.setdefault(category, {}).setdefault(level, set()).update(hits)


def scan_evidence(text: str) -> dict:
    """按语境给每个能力命中打等级：{类别: {等级: [命中片段]}}。"""
    out: dict = {}
    in_fence = False
    fence_level = "instructional"
    prose: list = []
    for line in (text or "").splitlines():
        match = FENCE_RE.match(line)
        if match:
            if not in_fence:
                in_fence = True
                recent = prose[-EVIDENCE_WINDOW:]
                # 明确标了"例如/示例"的代码块 = 示例；否则按"要你执行的代码"算（保守）
                fence_level = ("example" if any(EXAMPLE_MARKER.search(x) for x in recent)
                               else "instructional")
                _record(out, _line_hits(line), fence_level)   # ```bash 这一行本身也是信号
                prose = []
                continue
            in_fence = False
            continue
        if in_fence:
            _record(out, _line_hits(line), fence_level)
            continue
        recent = prose[-EVIDENCE_WINDOW:]
        if EXAMPLE_MARKER.search(line) or any(EXAMPLE_MARKER.search(x) for x in recent):
            level = "example"
        elif INSTRUCTION_VERB.search(line) or any(INSTRUCTION_VERB.search(x) for x in recent):
            level = "instructional"
        else:
            level = "mention"
        _record(out, _line_hits(line), level)
        prose.append(line)
    return out


def strongest_level(levels: dict) -> str:
    best = "mention"
    for level in levels:
        if EVIDENCE_RANK.get(level, 0) > EVIDENCE_RANK[best]:
            best = level
    return best


def dir_evidence(root: str) -> dict:
    """扫一个技能目录：{类别: {"level": 最强等级, "hits": {等级: [片段]}}}。"""
    merged: dict = {}
    for rel, path in skills.skill_files(root).items():
        if os.path.splitext(rel)[1].lower() not in TEXT_EXTS:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for category, levels in scan_evidence(text).items():
            bucket = merged.setdefault(category, {})
            for level, hits in levels.items():
                bucket.setdefault(level, set()).update(hits)
    return {category: {"level": strongest_level(levels),
                       "hits": {lvl: sorted(v) for lvl, v in sorted(levels.items())}}
            for category, levels in merged.items()}


def strong_categories(root: str) -> set:
    """有 declared/instructional 级证据的能力类别（治理只认这些）。"""
    return {category for category, info in dir_evidence(root).items()
            if EVIDENCE_RANK[info["level"]] >= EVIDENCE_RANK["instructional"]}


def signal_diff(local_dir: str, other_dir: str) -> dict:
    """上游版相对本地版：新增了哪些能力信号、消失了哪些。"""
    local, other = dir_signals(local_dir), dir_signals(other_dir)
    new, gone = {}, {}
    for category in set(local) | set(other):
        added = [x for x in other.get(category, []) if x not in local.get(category, [])]
        removed = [x for x in local.get(category, []) if x not in other.get(category, [])]
        if added:
            new[category] = added
        if removed:
            gone[category] = removed
    return {"new": new, "gone": gone}


def file_diff(local_dir: str, other_dir: str, max_lines: int = 40) -> dict:
    """文件级差异 + 统一 diff 片段。"""
    compared = skills.compare_dirs(local_dir, other_dir)
    # compare_dirs 的口径：only_a = 本地独有，only_b = 上游独有，differ = 两边都有但内容不同
    return {"added": compared["only_b"], "removed": compared["only_a"],
            "changed": compared["differ"],
            "lines": skills.diff_preview(local_dir, other_dir, max_lines=max_lines)}


def diff_skill(local_dir: str, other_dir: str, max_lines: int = 40) -> dict:
    files = file_diff(local_dir, other_dir, max_lines=max_lines)
    signals = signal_diff(local_dir, other_dir)
    files["identical"] = not (files["added"] or files["removed"] or files["changed"])
    return {"files": files, "signals": signals,
            "new_high_risk": sorted(c for c in signals["new"] if c in HIGH_RISK)}


def risky(report: dict) -> bool:
    return any(item.get("new_high_risk") for item in report.get("skills") or [])


def collect(cfg, name: str | None = None, pending_only: bool = True, log=print,
            max_lines: int = 40) -> dict:
    """收集差异报告：默认只看已暂存的版本（不联网）。

    指定 `name` 且没有暂存版、又允许联网时，会去上游取快照再比。
    """
    report: dict = {"skills": [], "source": "pending", "notes": []}
    local_of = {n: p for n, p, _ in skills.all_skills(cfg)}
    pending = {x["name"]: x["path"] for x in skills.pending_items(cfg)}
    wanted = [name] if name else sorted(pending)

    for skill_name in wanted:
        local_dir = local_of.get(skill_name)
        if not local_dir:
            report["notes"].append(f"{skill_name}：本地找不到这个技能，跳过")
            continue
        other_dir = pending.get(skill_name)
        origin = "已暂存的上游版"
        if not other_dir and not pending_only:
            other_dir, origin = _fetch_upstream_dir(cfg, skill_name, log=log)
        if not other_dir:
            report["notes"].append(f"{skill_name}：没有待审版本"
                                   + ("（加 --fetch 可去上游取）" if pending_only else "（上游没取到）"))
            continue
        item = diff_skill(local_dir, other_dir, max_lines=max_lines)
        item.update({"name": skill_name, "local": local_dir, "other": other_dir, "origin": origin})
        report["skills"].append(item)
    if not wanted and not report["skills"]:
        report["notes"].append("没有待审的技能（`_pending/` 为空）——先跑 `aml patrol check` 看有没有更新")
    return report


def _fetch_upstream_dir(cfg, skill_name: str, log=print):
    """去上游取该技能所在仓库的快照，返回 (目录, 说明)。失败返回 (None, 原因)。"""
    groups, _gits = update.group_by_repo(cfg)          # 它返回 (按仓库分组的 dict, git 安装的 list)
    for (repo, branch), items in groups.items():
        for found_name, _local_dir, meta, _mpath in items:
            if found_name != skill_name:
                continue
            try:
                root, _used, scratch = github.fetch_repo(cfg, repo, branch)
            except Exception as e:  # noqa: BLE001
                return None, f"上游取不到（{type(e).__name__}）"
            subdir = meta.get("subdir") or ""
            path = os.path.join(root, subdir) if subdir else root
            return (path if os.path.isdir(path) else None), f"{repo}@{branch}（临时快照）"
    return None, "这个技能没有记录上游来源"


def render(report: dict) -> str:
    lines = []
    for item in report["skills"]:
        files = item["files"]
        lines.append(f"技能 {item['name']}：本地 vs {item['origin']}")
        if files["identical"]:
            lines.append("  内容一致（无差异）")
        else:
            lines.append(f"  文件：新增 {len(files['added'])} / 删除 {len(files['removed'])} / "
                         f"改动 {len(files['changed'])}")
            for rel in files["added"][:8]:
                lines.append(f"    + {rel}")
            for rel in files["removed"][:8]:
                lines.append(f"    - {rel}")
            for rel in files["changed"][:8]:
                lines.append(f"    ~ {rel}")
        if files["lines"]:
            lines.append("  diff：")
            lines += [f"    {row}" for row in files["lines"]]

        new, gone = item["signals"]["new"], item["signals"]["gone"]
        labels = {c: label for c, label, _ in RISK_PATTERNS}
        if new:
            lines.append("  ⚠ 新增能力信号（照做会改变系统状态）：")
            for category, hits in sorted(new.items()):
                flag = "高风险" if category in HIGH_RISK else "留意"
                lines.append(f"    [{category}/{flag}] {labels.get(category, '')}：{'；'.join(hits[:4])}")
        if gone:
            lines.append("  能力信号消失：")
            for category, hits in sorted(gone.items()):
                lines.append(f"    [{category}] {'；'.join(hits[:3])}")
        if item["new_high_risk"]:
            lines.append(f"  结论：有 {len(item['new_high_risk'])} 类**新增高风险能力**"
                         f"（{', '.join(item['new_high_risk'])}）—— 采纳前请人工过目，"
                         f"确认要就 `aml patrol accept {item['name']}`")
        elif not files["identical"]:
            lines.append("  结论：只是内容变化，没有新增高风险能力信号")
        lines.append("")
    for note in report.get("notes") or []:
        lines.append(f"· {note}")
    if not report["skills"] and not report.get("notes"):
        lines.append("没有可审的技能")
    return "\n".join(lines).rstrip()
