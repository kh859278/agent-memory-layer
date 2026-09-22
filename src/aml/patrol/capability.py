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
    # tracked → approved 必须有（2026-09-22）：推断出来的干净技能现在停在 tracked，
    # 而闸门给用户的恢复命令就是 `patrol lifecycle <名> approved` —— 这条路要 --force
    # 才走通的话，等于把"正确做法"藏在一条会被拒的命令后面。
    "tracked": ("candidate", "scanned", "approved", "deprecated", "retired"),
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
# 自动更新还要**有人给过的批准凭据**（2026-09-22 加，外部评审的第一条）：
# 状态本身不算证据 —— `infer_state()` 会把"有元数据 + 本地干净"的技能推断成 active，
# 那等于让从没被人看过的技能白拿自动更新权（本机实测 26/38 个技能正是这么来的）。
# 凭据就是 `baseline_hash`：`record_approval()` 写，`accept` 与人工 `set_state(by="human")` 都会写。
APPROVAL_FIELD = "baseline_hash"
# 但"要不要凭据"是**用户的策略选择**，不是我们能替他定的：
# `patrol.update.approval_required: false` 回到"本地干净就自动更新"（2026-09-22 用户明确要这个）。
# 默认 true —— 默认值只代表"什么都不说时的取向"，不代表作者替你决定。
APPROVAL_POLICY_KEY = "approval_required"


def approval_required(cfg) -> bool:
    """自动更新要不要批准凭据？读 `patrol.update.approval_required`（默认 **true**）。

    拿不准一律按 true：`cfg` 是 None、配置段畸形、值缺失 —— 全都走更严的那条路。
    """
    try:
        update = (cfg.section("patrol") or {}).get("update")
    except Exception:  # noqa: BLE001 - 配置畸形不该让闸门变成"放行"
        return True
    if not isinstance(update, dict):
        return True
    return bool(update.get(APPROVAL_POLICY_KEY, True))


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


def infer_state(meta: dict | None, strict: bool = True) -> str:
    """给"从没登记过"的技能一个诚实的初始状态。

    `strict=True`（默认，对应 `approval_required: true`）：只有"受跟踪 + 本地干净 +
    没有待批的上游新版"才认为它在用（`tracked`，**不在 AUTO_OK**）——
    2026-09-22 改的原因见模块里 `APPROVAL_POLICY_KEY` 那段注释。

    `strict=False`（用户在配置里显式要自动更新）：干净技能推断成 `active`，
    也就是 2026-09-22 之前的行为。
    """
    meta = meta or {}
    if not meta:
        return "discovered"
    if meta.get("local_patch") or meta.get("local_diff"):
        return "candidate"
    if meta.get("staged_upstream"):
        return "scanned"
    return "tracked" if strict else "active"


def has_approval(entry: dict | None) -> bool:
    """这个人给的批准凭据在不在？（判据只有 `baseline_hash` 一条）

    为什么不在 `set_state` 里顺手放宽成"状态是人设的就算批准"：那样 `inferred` 标记、
    history 的 `by` 字段都会变成安全边界，多一个判据就多一条漏路。凭据只有一种写法、
    一处读取，最好审。
    """
    return bool((entry or {}).get(APPROVAL_FIELD))


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
        state = infer_state(meta, strict=approval_required(cfg))
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
    if target in AUTO_OK and by == "human":
        # 人**显式**把技能设成可自动更新 = 一次批准：补上审批凭据。
        # 不补的话 gate 会拦下（状态是人给的、凭据却缺失），人会觉得"我明明设成 active 了"。
        local = next((path for name_, path, _ in skills.all_skills(cfg) if name_ == name), None)
        if local:
            caps = sorted(strong_capabilities(local))
            entry[APPROVAL_FIELD] = skills.dir_hash(local)
            entry["approved_capabilities"] = caps
            entry["capability_history"] = sorted(set(entry.get("capability_history") or []) | set(caps))
            log(f"    已补批准凭据（绑定内容 {entry[APPROVAL_FIELD][:7]}，"
                f"能力 {caps or '（无）'}）—— 自动更新只认这个凭据")
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

    **批准凭据这一条是策略开关**（2026-09-22 晚）：默认 `approval_required: true`，
    要批准凭据（`baseline_hash`），干净技能只推断到 `tracked`；
    配置里写 `patrol.update.approval_required: false` 就回到"本地干净即自动更新"，
    这时干净技能推断成 `active`。两条路都保留"本地改动永不覆盖""上游新增未声明高危能力不覆盖"。

    `meta` 传调用方手上那份最新的（`update_one` 里就有），比重新读盘准。

    2026-09-21 加的三条（都源自外部评审，且都落在"更新治理"能管的范围内）：
      · **批准绑内容 hash**：本地内容在上次批准之后变过 → 批准作废，重新过目
      · **敏感能力"删掉又回来"** → 重新批准（不能靠"这次 diff 里没有新增"蒙过去）
      · **只认 instructional 及以上证据**：示例里的 shell 不再触发拦截（治误报）
    """
    require_approval = approval_required(cfg)
    data = load(cfg)
    if name not in (data["skills"] or {}):
        if meta is None:
            meta = skills.read_meta(local_dir)[0] or {}
        state = infer_state(meta, strict=require_approval)
        _register(cfg, data, name, state, "首次登记（按本地/上游状态推断）")
        save(cfg, data)
    state = state_of(cfg, name)
    entry = (data["skills"] or {}).get(name) or {}
    declaration = read_declaration(local_dir)
    reasons = []
    if state in BLOCKED:
        reasons.append(f"生命周期状态 {state}：不更新、不加载")
    elif state not in AUTO_OK:
        if state == "candidate":
            # candidate = 本地改过（local_patch/local_diff）。说清是这个原因，
            # 别让"只暂存"被读成"这技能没人看过"——用户会以为批准一下就能自动更新。
            reasons.append("生命周期状态 candidate：本地有改动（local_patch/local_diff），"
                           "只暂存待批")
        else:
            reasons.append(f"生命周期状态 {state}：还没被人看过，只暂存待批")
    elif require_approval and not has_approval(entry):
        # 只有策略要求凭据时才拦；恢复命令不在这里重复写（下面统一给一条）
        reasons.append("没有批准凭据（这个技能从没被人批准过，现有状态是推断出来的）")
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
    if reasons and state not in BLOCKED:
        # 只拦不说怎么放行 = 把人卡住。但**放行方式取决于拦的理由**：
        # candidate 是"本地改过"，批准/规定状态都不解决问题，只能人工决定要不要覆盖。
        if state == "candidate":
            reasons.append(f"想用上游版本覆盖本地改动：`aml patrol diff {name}` 看差异后 "
                           f"`aml patrol accept {name}`（覆盖前会先备份本地版本）")
        else:
            reasons.append(f"恢复自动更新：`aml patrol diff {name}` 审过上游改了什么之后，"
                           f"`aml patrol lifecycle {name} approved`（或 `aml patrol accept {name}`）")
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
    if approval_required(cfg):
        lines.append("  说明：只有 approved / active 允许自动更新，**而且必须有批准凭据**"
                     "（baseline_hash：`aml patrol accept` 或 `aml patrol lifecycle <名> approved`"
                     " 才会写）；其余状态一律只暂存待批。批准会绑定内容 hash，本地一改就作废")
    else:
        lines.append("  说明：`patrol.update.approval_required: false` —— **本地干净的技能"
                     "直接自动更新**（你显式选的策略）；本地改动永不覆盖、上游新增未声明的高危能力"
                     "仍然会拦下")
    lines.append("  证据分级：declared（声明）> instructional（要求执行）> example（示例）"
                 "> mention（只是提到）；闸门只认前两级 —— 示例里的 shell 不再触发拦截")
    return "\n".join(lines)


__all__ = ["DECL_NAME", "STATES", "TRANSITIONS", "AUTO_OK", "BLOCKED", "APPROVAL_FIELD",
           "APPROVAL_POLICY_KEY", "REVIEW_FILE",
           "decl_path", "read_declaration", "normalize_declaration", "compare",
           "new_high_risk", "strong_capabilities", "has_approval", "approval_required",
           "state_file", "load", "save",
           "state_of",
           "infer_state", "ensure", "can_transition", "set_state", "gate", "overview",
           "render", "mark_reviewed", "reviewed_hash", "review_file", "record_approval",
           "accept_risk"]
