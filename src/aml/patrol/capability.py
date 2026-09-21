"""技能能力声明（`skill.yaml`）与生命周期状态机。

外部 review 的第 3 条缺口说得很准：**现在只有"更新治理"，没有能力模型**。
`patrol diff` 能看出"上游新版多了一处 shell 调用"，但它只是**事后信号**，
既不知道这个技能"本来就该有什么能力"，也没有"这个技能现在处于什么阶段"的概念。
于是两个真问题无解：

  1. **能力差异 vs 声明**：技能声明只用 shell，上游新版悄悄加了网络请求 ——
     光看 commit 变化看不出来，只有"声明 ∩ 实测"才能。
  2. **谁能自动更新**：一个刚纳管、从没被人看过的技能，和一个用了半年、人审过的技能，
     现在待遇完全一样（只要本地干净就自动覆盖）。

## 声明长什么样（放在技能目录下，与 `SKILL.md` 同级）

```yaml
capabilities: [shell, network]   # 声明它会用到哪些能力
scope: "只处理本仓库文档"          # 一句话说清作用范围（人看的）
requires_approval: true          # 每次采纳上游版本都要人工批准
authority: upstream              # 谁写的：human / upstream / generated / mixed
```

`capabilities` 取值与 `patrol.diff` 的信号类别同名（shell / network / secrets /
filesystem-write / git-write / install / browser / url），这样"声明"与"实测"能直接比。

## 生命周期

```
discovered → tracked → candidate → scanned → approved → active → deprecated → disabled → retired
                                              ↑                                    │
                                              └──────────── active ←───────────────┘
```

* `approved` / `active` —— **只有这两个状态允许被自动更新、被自动加载**
  （程序性内容本来就不进通用检索，这是第二道闸门，见 `docs/TRUST-MODEL.md`）
* `deprecated` / `disabled` / `retired` —— 不更新、不加载；`retired` 是终态
* 其余状态 = "还没被人看过" → 一律只暂存待批，不自动覆盖

状态存在 `$AML_HOME/state/patrol/lifecycle.json`（每次变更都留一条 history，
"谁在什么时候为什么把它改成这个状态"要能查 —— 这正是 review 说的缺 provenance）。
"""
from __future__ import annotations

import datetime as dt
import json
import os

from . import diff, skills

DECL_NAME = "skill.yaml"
STATE_FILE = "lifecycle.json"

STATES = ("discovered", "tracked", "candidate", "scanned", "approved", "active",
          "deprecated", "disabled", "retired")

# 允许的状态迁移（人/agent 只能沿这些边走；要跳着走必须 force，并写进 history）
TRANSITIONS = {
    "discovered": ("tracked", "retired"),
    "tracked": ("candidate", "scanned", "retired"),
    "candidate": ("scanned", "approved", "retired"),
    "scanned": ("approved", "candidate", "retired"),
    "approved": ("active", "deprecated", "retired"),
    "active": ("deprecated", "disabled", "retired", "candidate"),
    "deprecated": ("disabled", "active", "retired"),
    "disabled": ("active", "retired"),
    "retired": (),
}
# 只有这两个状态可以自动更新；其余要么停用，要么"还没被人看过"
AUTO_OK = ("approved", "active")
BLOCKED = ("deprecated", "disabled", "retired")


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 声明

def decl_path(skill_dir: str) -> str:
    return os.path.join(skill_dir, DECL_NAME)


def read_declaration(skill_dir: str) -> dict:
    """读 `skill.yaml`；没有就返回 {}（**没声明 ≠ 声明为空**，调用方要分清）。"""
    path = decl_path(skill_dir)
    if not os.path.isfile(path):
        return {}
    import yaml
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 - 声明坏了不该让整轮巡检崩
        return {"_error": "解析失败"}
    return normalize_declaration(raw)


def normalize_declaration(raw: dict) -> dict:
    caps = raw.get("capabilities") or []
    if isinstance(caps, str):
        caps = [c.strip() for c in caps.replace(",", " ").split() if c.strip()]
    known = {category for category, _label, _pattern in diff.RISK_PATTERNS}
    return {
        "capabilities": sorted({str(c).strip() for c in caps if str(c).strip()}),
        "unknown_capabilities": sorted({str(c).strip() for c in caps
                                        if str(c).strip() not in known}),
        "scope": str(raw.get("scope") or "").strip(),
        "requires_approval": bool(raw.get("requires_approval", False)),
        "authority": str(raw.get("authority") or "").strip(),
    }


def compare(skill_dir: str) -> dict:
    """声明 vs 实测：**只有 declared/instructional 级证据**才算"真在用这个能力"。

    为什么分等级（2026-09-21 收紧）：纯正则分不清"叫你执行"和"举了个例子" ——
    第一版把 Markdown 引用块 `>` 当成写文件、把行内反引号当成 shell，全是误报。
    现在：`undeclared_high_risk`（会被闸门拦）只收 instructional 级；
    只在示例里出现的进 `example_only_high_risk`，**只提示不拦**（等级说明见 `patrol/diff.py`）。
    """
    declared = read_declaration(skill_dir)
    detected = diff.dir_signals(skill_dir)
    evidence = diff.dir_evidence(skill_dir)
    levels = {category: info["level"] for category, info in evidence.items()}
    strong = diff.strong_categories(skill_dir)
    caps = set(declared.get("capabilities") or [])
    undeclared = sorted(c for c in detected if c not in caps)
    return {
        "declared": declared,
        "detected": detected,
        "evidence": evidence,
        "levels": levels,
        "undeclared": undeclared,
        "undeclared_high_risk": sorted(c for c in undeclared
                                       if c in diff.HIGH_RISK and c in strong),
        "example_only_high_risk": sorted(c for c in undeclared
                                         if c in diff.HIGH_RISK and c not in strong),
        "declared_but_unused": sorted(c for c in caps if c not in detected),
        "has_declaration": bool(declared) and not declared.get("_error"),
    }


def new_high_risk(local_dir: str, up_dir: str, declared: dict | None = None) -> list:
    """上游新版相对本地版**新增**的高风险能力里，声明里没有的那些。

    只看 instructional 及以上证据（示例级不算），而且要求**等级确实变强了** ——
    否则"本地本来就有的 shell"会被每一轮都报成新增。
    """
    caps = set((declared or {}).get("capabilities") or [])
    before, after = diff.dir_evidence(local_dir), diff.dir_evidence(up_dir)
    out = []
    for category in diff.HIGH_RISK:
        if category in caps:
            continue
        level_after = (after.get(category) or {}).get("level", "mention")
        level_before = (before.get(category) or {}).get("level", "mention")
        if (diff.EVIDENCE_RANK[level_after] >= diff.EVIDENCE_RANK["instructional"]
                and diff.EVIDENCE_RANK[level_after] > diff.EVIDENCE_RANK[level_before]):
            out.append(category)
    return sorted(out)


def strong_capabilities(skill_dir: str, declaration: dict | None = None) -> set:
    """这个技能当前"算数"的能力集合 = 声明 ∪ instructional 及以上证据。"""
    caps = set((declaration or read_declaration(skill_dir)).get("capabilities") or [])
    return caps | diff.strong_categories(skill_dir)


# ------------------------------------------------------------------ 生命周期状态

def state_file(cfg):
    return cfg.state_dir / "patrol" / STATE_FILE


def load(cfg) -> dict:
    path = state_file(cfg)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"skills": {}}
    data.setdefault("skills", {})
    return data


def save(cfg, data: dict) -> str:
    path = state_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return str(path)


def state_of(cfg, name: str) -> str:
    entry = (load(cfg).get("skills") or {}).get(name) or {}
    return entry.get("state") or "discovered"


def infer_state(meta: dict | None) -> str:
    """给"从没登记过"的技能一个诚实的初始状态（宁可保守）。

    保守的含义：只有"受跟踪 + 本地干净 + 没有待批的上游新版"才认为它在用（`active`），
    其余都给一个"还没被人看过"的状态 —— 反正这些状态都会走"只暂存"的路径。
    """
    meta = meta or {}
    if not meta:
        return "discovered"
    if meta.get("local_patch") or meta.get("local_diff"):
        return "candidate"
    if meta.get("staged_upstream"):
        return "scanned"
    return "active"


def _register(cfg, data: dict, name: str, state: str, why: str) -> None:
    data["skills"][name] = {"state": state, "since": _now(), "inferred": True,
                            "history": [{"from": None, "to": state, "at": _now(),
                                         "why": why, "by": "auto"}]}


def ensure(cfg, log=print) -> dict:
    """把"库里已有的技能"补登记进状态表（不动已登记过的状态）。"""
    data = load(cfg)
    skills_map = data["skills"]
    added = {}
    for name, path, _root in skills.all_skills(cfg):
        if name in skills_map:
            continue
        meta, _ = skills.read_meta(path)
        state = infer_state(meta)
        _register(cfg, data, name, state, "首次登记（按本地/上游状态推断）")
        added[name] = state
    if added:
        save(cfg, data)
        log(f"  生命周期首次登记 {len(added)} 个技能（"
            + "、".join(f"{k}:{v}" for k, v in sorted(added.items())[:6])
            + ("…" if len(added) > 6 else "") + "）")
    return added


def can_transition(current: str, target: str) -> bool:
    return target in (TRANSITIONS.get(current) or ())


def set_state(cfg, name: str, target: str, why: str = "", by: str = "human",
              force: bool = False, log=print) -> dict:
    """改状态：默认只允许合法迁移；`force` 可越级但会写进 history。"""
    if target not in STATES:
        return {"ok": False, "error": f"未知状态 {target!r}，可选：{'/'.join(STATES)}"}
    data = load(cfg)
    entry = data["skills"].setdefault(name, {"state": "discovered", "since": _now(),
                                             "history": []})
    current = entry.get("state") or "discovered"
    if current == target:
        return {"ok": True, "state": target, "unchanged": True}
    if not can_transition(current, target) and not force:
        return {"ok": False, "error": f"{current} → {target} 不是合法迁移"
                                      f"（允许：{', '.join(TRANSITIONS.get(current) or ['（终态）'])}）；"
                                      f"确实要跳就用 --force",
                "allowed": list(TRANSITIONS.get(current) or [])}
    entry["state"] = target
    entry["since"] = _now()
    entry.setdefault("history", []).append({"from": current, "to": target, "at": _now(),
                                            "why": why, "by": by, "forced": bool(force)})
    save(cfg, data)
    log(f"  {name}: {current} → {target}" + (f"（{why}）" if why else ""))
    return {"ok": True, "state": target, "from": current}


# ------------------------------------------------------------------ 审阅记录与批准

REVIEW_FILE = "reviewed.json"


def review_file(cfg):
    return cfg.state_dir / "patrol" / REVIEW_FILE


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def mark_reviewed(cfg, name: str, staged_dir: str, by: str = "human") -> dict:
    """记下"这一版**被人看过**了"（内容是暂存目录的内容指纹）。

    为什么需要它：`patrol diff` 审的是 A，`patrol accept` 采纳的可能是 B ——
    中间任何一个环节让暂存目录变了（重新暂存、别的会话动了文件），
    人就会在"以为审过"的情况下批准没审过的内容。有了记录，accept 能直接对上。
    """
    path = review_file(cfg)
    data = _load_json(path, {})
    entry = {"hash": skills.dir_hash(staged_dir), "dir": str(staged_dir),
             "at": _now(), "by": by}
    data[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return entry


def reviewed_hash(cfg, name: str):
    return (_load_json(review_file(cfg), {}).get(name) or {}).get("hash")


def record_approval(cfg, name: str, skill_dir: str, meta: dict | None = None,
                    by: str = "human", why: str = "", log=print) -> dict:
    """人工批准一个技能版本：状态转 approved，并把批准**绑在内容 hash 上**。

    三样东西一起写：
      · `baseline_hash` —— 批准的是哪一版内容；本地内容一变，批准自动作废
      · `approved_capabilities` —— 批准时"算数"的能力集合（声明 ∪ instructional 证据）
      · `capability_history` —— 历史上出现过的能力并集；敏感能力"删掉又回来"要重新批准
    """
    info = compare(skill_dir)
    caps = strong_capabilities(skill_dir, info["declared"])
    data = load(cfg)
    entry = data["skills"].setdefault(name, {"state": "discovered", "since": _now(),
                                             "history": []})
    current = entry.get("state") or "discovered"
    entry["state"] = "approved"
    entry["since"] = _now()
    entry["baseline_hash"] = (meta or {}).get("content_hash") or skills.dir_hash(skill_dir)
    entry["approved_capabilities"] = sorted(caps)
    entry["capability_history"] = sorted(set(entry.get("capability_history") or []) | caps)
    entry.setdefault("history", []).append({
        "from": current, "to": "approved", "at": _now(),
        "why": why or "人工批准这一版内容", "by": by,
        "baseline_hash": entry["baseline_hash"], "capabilities": sorted(caps)})
    save(cfg, data)
    log(f"  已批准 {name}（状态 {current} → approved；能力 {sorted(caps) or '（无）'}；"
        f"绑定内容 {entry['baseline_hash'][:7]}）")
    return entry


def accept_risk(cfg, name: str, local_dir: str, staged_dir: str) -> dict:
    """采纳前摆给人看：能力差异 + 需要显式确认的风险项。

    注意**生命周期状态只提示、不拦** —— `accept` 本身就是人的批准动作，
    状态只是"这个技能以前没人看过"的记账；真正的拦截理由是**内容里的新风险**。
    """
    info = compare(staged_dir)
    before = compare(local_dir)
    declaration = info["declared"]
    risky = new_high_risk(local_dir, staged_dir, declaration)
    reasons = []
    if risky:
        reasons.append("新增未声明的高危能力：" + ", ".join(risky))
    if declaration.get("requires_approval"):
        reasons.append("skill.yaml 声明 requires_approval")
    state = state_of(cfg, name)
    lines = [f"能力：声明 {declaration.get('capabilities') or '（无 skill.yaml）'}；"
             f"实测等级 {info['levels'] or '（没扫到信号）'}"]
    if before["levels"] != info["levels"]:
        lines.append(f"  等级变化：{before['levels'] or '（无）'} → {info['levels'] or '（无）'}")
    if info["example_only_high_risk"]:
        lines.append(f"  提示（只有示例级证据，不拦）：{', '.join(info['example_only_high_risk'])}")
    if state not in AUTO_OK:
        lines.append(f"  提示：生命周期状态为 {state}（第一次批准会建立 baseline）")
    return {"reasons": reasons, "lines": lines, "info": info, "new_high_risk": risky}


# ------------------------------------------------------------------ 闸门

def gate(cfg, name: str, local_dir: str, up_dir: str | None = None,
         meta: dict | None = None) -> dict:
    """这次能不能自动动这个技能？返回 {allow, reasons, declaration, state}。

    `allow=False` 的含义是"**只暂存，等人批**"，不是"禁止" ——
    人工 `aml patrol accept` 永远可以采纳（人就是那道批准）。

    没登记过的技能**就地按 meta 推断登记**（而不是一律拦下）：否则升级当天，
    所有"本地干净、一直在跟踪"的老技能都会被拦一轮，那是纯粹的自伤。
    `meta` 传调用方手上那份最新的（`update_one` 里就有），比重新读盘准。

    2026-09-21 加的三条（都源自外部评审，且都落在"更新治理"能管的范围内）：
      · **批准绑内容 hash**：本地内容在上次批准之后变过 → 批准作废，重新过目
      · **敏感能力"删掉又回来"** → 重新批准（不能靠"这次 diff 里没有新增"蒙过去）
      · **只认 instructional 及以上证据**：示例里的 shell 不再触发拦截（治误报）
    """
    data = load(cfg)
    if name not in (data["skills"] or {}):
        if meta is None:
            meta = skills.read_meta(local_dir)[0] or {}
        state = infer_state(meta)
        _register(cfg, data, name, state, "首次登记（按本地/上游状态推断）")
        save(cfg, data)
    state = state_of(cfg, name)
    entry = (data["skills"] or {}).get(name) or {}
    declaration = read_declaration(local_dir)
    reasons = []
    if state in BLOCKED:
        reasons.append(f"生命周期状态 {state}：不更新、不加载")
    elif state not in AUTO_OK:
        reasons.append(f"生命周期状态 {state}：还没被人看过，只暂存待批")
    if declaration.get("requires_approval"):
        reasons.append("skill.yaml 声明要求人工批准")

    baseline = entry.get("baseline_hash")
    if baseline:
        current_hash = (meta or {}).get("content_hash") or skills.dir_hash(local_dir)
        if current_hash != baseline:
            reasons.append("本地内容在上次批准之后变过（批准绑的是内容 hash，已失效，"
                           "需重新过目后再批准）")

    approved = set(entry.get("approved_capabilities") or [])
    history = set(entry.get("capability_history") or [])
    now_strong = strong_capabilities(local_dir, declaration)
    returned = sorted((history - approved) & now_strong & set(diff.HIGH_RISK))
    if returned:
        reasons.append("敏感能力重新出现（历史上出现过、批准时没有、现在又有）："
                       + ", ".join(returned))

    if up_dir:
        risky = new_high_risk(local_dir, up_dir, declaration)
        if risky:
            reasons.append(f"上游新版新增未声明的能力：{', '.join(risky)}")
    return {"allow": not reasons, "reasons": reasons, "state": state,
            "declaration": declaration, "blocked": state in BLOCKED}


# ------------------------------------------------------------------ 人看的视图

def overview(cfg) -> dict:
    """每个技能一行：状态 + 声明的能力 + 证据等级 + 批准绑定的内容。"""
    data = load(cfg)
    rows = []
    for name, path, _root in skills.all_skills(cfg):
        entry = data["skills"].get(name) or {}
        info = compare(path)
        rows.append({
            "name": name,
            "state": entry.get("state") or "discovered",
            "since": entry.get("since"),
            "declared": info["declared"].get("capabilities") or [],
            "levels": info["levels"],
            "undeclared": info["undeclared"],
            "undeclared_high_risk": info["undeclared_high_risk"],
            "example_only_high_risk": info["example_only_high_risk"],
            "requires_approval": bool(info["declared"].get("requires_approval")),
            "has_declaration": info["has_declaration"],
            "baseline_hash": entry.get("baseline_hash"),
            "approved_capabilities": entry.get("approved_capabilities") or [],
        })
    return {"skills": rows, "states": {s: sum(1 for r in rows if r["state"] == s) for s in STATES}}


def render(cfg) -> str:
    data = overview(cfg)
    rows = data["skills"]
    if not rows:
        return "没有可治理的技能（检查 patrol.skill_roots 配置）"
    counts = "、".join(f"{s} {n}" for s, n in data["states"].items() if n)
    lines = [f"技能生命周期（{len(rows)} 个）：{counts}"]
    for row in sorted(rows, key=lambda r: (r["state"] != "active", r["name"]))[:40]:
        flags = []
        flags.append("有声明" if row["has_declaration"] else "无声明")
        if row["requires_approval"]:
            flags.append("需批准")
        if row["baseline_hash"]:
            flags.append("已批准")
        if row["undeclared_high_risk"]:
            flags.append(f"⚠未声明高危（{','.join(row['undeclared_high_risk'])}）")
        elif row["example_only_high_risk"]:
            flags.append(f"仅示例级（{','.join(row['example_only_high_risk'])}）")
        elif row["undeclared"]:
            flags.append(f"未声明：{','.join(row['undeclared'])}")
        lines.append(f"  [{row['state']:<11}] {row['name']:<24} {'；'.join(flags)}")
    if len(rows) > 40:
        lines.append(f"  …（还有 {len(rows) - 40} 个，用 --json 看全量）")
    lines.append("  说明：只有 approved / active 允许自动更新；其余状态一律只暂存待批"
                 "（`aml patrol accept` 就是人工批准，批准会绑定内容 hash）")
    lines.append("  证据分级：declared（声明）> instructional（要求执行）> example（示例）"
                 "> mention（只是提到）；闸门只认前两级 —— 示例里的 shell 不再触发拦截")
    return "\n".join(lines)


__all__ = ["DECL_NAME", "STATES", "TRANSITIONS", "AUTO_OK", "BLOCKED", "REVIEW_FILE",
           "decl_path", "read_declaration", "normalize_declaration", "compare",
           "new_high_risk", "strong_capabilities", "state_file", "load", "save", "state_of",
           "infer_state", "ensure", "can_transition", "set_state", "gate", "overview",
           "render", "mark_reviewed", "reviewed_hash", "review_file", "record_approval",
           "accept_risk"]
