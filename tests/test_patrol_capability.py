"""能力声明（skill.yaml）与生命周期状态机的测试（不联网、真临时目录）。

要钉死的口径：
  · **没声明 ≠ 声明为空**：没有 `skill.yaml` 与"声明了些什么"必须能被区分
  · 只有 `approved` / `active` 允许自动更新；其余状态只暂存
  · 越级迁移默认被拒；`--force` 才行，且 history 里留痕
  · 上游新版新增"未声明的高风险能力" → 不自动覆盖（这是能力模型存在的理由）
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml.patrol import capability, skills, update  # noqa: E402

SHELL_BODY = "跑一下这个：\n\n```bash\necho hello\n```\n"
PLAIN_BODY = "这一版只写文档，不碰系统。\n"


def make_skill(root, name, body=PLAIN_BODY, decl=None, tracked=False):
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: 测试技能\n---\n\n{body}\n")
    if decl is not None:
        import yaml
        with open(os.path.join(path, capability.DECL_NAME), "w", encoding="utf-8",
                  newline="\n") as f:
            yaml.safe_dump(decl, f, allow_unicode=True)
    if tracked:
        skills.write_meta(path, {"name": name, "repo": "r", "subdir": name,
                                 "content_hash": skills.dir_hash(path), "local_diff": False})
    return path


def make_cfg(tmp_path, roots):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["skill_roots"] = [{"name": f"root{i}", "path": str(root),
                                          "mirror_to": f"技能原始/root{i}"}
                                         for i, root in enumerate(roots)]
    cfg.data["patrol"]["snapshot_dirs"] = []
    return cfg


# ------------------------------------------------------------------ 声明

def test_missing_declaration_is_not_an_empty_declaration(tmp_path):
    path = make_skill(str(tmp_path / "live"), "s1")
    assert capability.read_declaration(path) == {}
    info = capability.compare(path)
    assert info["has_declaration"] is False and info["declared"] == {}


def test_declaration_is_normalized(tmp_path):
    path = make_skill(str(tmp_path / "live"), "s1", decl={
        "capabilities": "shell, network", "scope": "只读仓库", "requires_approval": True,
        "authority": "human", "还有": "未知字段被忽略"})
    decl = capability.read_declaration(path)
    assert decl["capabilities"] == ["network", "shell"]
    assert decl["requires_approval"] is True and decl["scope"] == "只读仓库"


def test_broken_declaration_does_not_crash(tmp_path):
    path = make_skill(str(tmp_path / "live"), "s1")
    with open(os.path.join(path, capability.DECL_NAME), "w", encoding="utf-8") as f:
        f.write("capabilities: [shell\n")     # 故意写坏
    decl = capability.read_declaration(path)
    assert decl.get("_error") and capability.compare(path)["has_declaration"] is False


def test_compare_separates_declared_detected_and_undeclared(tmp_path):
    path = make_skill(str(tmp_path / "live"), "s1", body=SHELL_BODY,
                      decl={"capabilities": ["network"]})
    info = capability.compare(path)
    assert "shell" in info["detected"]
    assert info["undeclared"] == ["shell"] and info["undeclared_high_risk"] == ["shell"]
    assert info["declared_but_unused"] == ["network"]


# ------------------------------------------------------------------ 生命周期

def test_state_machine_allows_only_listed_transitions():
    assert capability.can_transition("tracked", "candidate")
    assert not capability.can_transition("discovered", "active")
    assert not capability.can_transition("retired", "active")      # 终态


def test_set_state_records_history_and_refuses_illegal_jump(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    make_skill(str(tmp_path / "live"), "s1")
    bad = capability.set_state(cfg, "s1", "active", log=lambda *_: None)
    assert bad["ok"] is False and "合法迁移" in bad["error"]
    assert capability.state_of(cfg, "s1") == "discovered"
    ok = capability.set_state(cfg, "s1", "tracked", why="纳管", by="tester", log=lambda *_: None)
    assert ok["ok"] and capability.state_of(cfg, "s1") == "tracked"
    forced = capability.set_state(cfg, "s1", "active", why="急用", force=True, log=lambda *_: None)
    assert forced["ok"] and capability.state_of(cfg, "s1") == "active"
    history = capability.load(cfg)["skills"]["s1"]["history"]
    assert history[0]["to"] == "tracked" and history[-1]["forced"] is True


def test_ensure_infers_state_without_touching_registered_ones(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    for name in ("clean", "dirty", "pending"):
        path = make_skill(str(tmp_path / "live"), name)
        meta = {"name": name, "local_diff": False}
        if name == "dirty":
            meta["local_patch"] = "我改过"
        if name == "pending":
            meta["staged_upstream"] = "/x"
        skills.write_meta(path, meta)
    make_skill(str(tmp_path / "live"), "unknown")           # 没有元数据 → 没纳管
    added = capability.ensure(cfg, log=lambda *_: None)
    assert added == {"clean": "active", "dirty": "candidate", "pending": "scanned",
                     "unknown": "discovered"}
    capability.set_state(cfg, "clean", "deprecated", why="停用", log=lambda *_: None)
    capability.ensure(cfg, log=lambda *_: None)             # 再跑一次不能把状态改回去
    assert capability.state_of(cfg, "clean") == "deprecated"


# ------------------------------------------------------------------ 闸门

def test_gate_allows_only_approved_or_active(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", tracked=True)
    up = make_skill(str(tmp_path / "up"), "s1", body=PLAIN_BODY)
    capability.ensure(cfg, log=lambda *_: None)
    assert capability.state_of(cfg, "s1") == "active"          # 受跟踪 + 本地干净
    assert capability.gate(cfg, "s1", live, up)["allow"] is True
    capability.set_state(cfg, "s1", "candidate", why="本地改过", log=lambda *_: None)
    verdict = capability.gate(cfg, "s1", live, up)
    assert verdict["allow"] is False and "还没被人看过" in verdict["reasons"][0]


def test_gate_blocks_new_undeclared_high_risk_capability(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY, tracked=True)
    up = make_skill(str(tmp_path / "up"), "s1", body=SHELL_BODY)
    capability.ensure(cfg, log=lambda *_: None)
    verdict = capability.gate(cfg, "s1", live, up)
    assert verdict["allow"] is False and "shell" in verdict["reasons"][0]
    # 声明过 shell 的话，同样的一版就放行（声明 = "我知道它会用 shell"）
    live2 = make_skill(str(tmp_path / "live2"), "s2", body=PLAIN_BODY,
                       decl={"capabilities": ["shell"]})
    up2 = make_skill(str(tmp_path / "up2"), "s2", body=SHELL_BODY)
    assert capability.new_high_risk(live2, up2, {"capabilities": ["shell"]}) == []


def test_gate_honors_requires_approval(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", decl={"requires_approval": True}, tracked=True)
    up = make_skill(str(tmp_path / "up"), "s1")
    capability.ensure(cfg, log=lambda *_: None)
    verdict = capability.gate(cfg, "s1", live, up)
    assert verdict["allow"] is False and "人工批准" in verdict["reasons"][0]


def test_gate_refuses_updates_for_disabled_skill(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", tracked=True)
    up = make_skill(str(tmp_path / "up"), "s1", body=SHELL_BODY)
    capability.ensure(cfg, log=lambda *_: None)
    capability.set_state(cfg, "s1", "deprecated", why="不用了", log=lambda *_: None)
    verdict = capability.gate(cfg, "s1", live, up)
    assert verdict["allow"] is False and verdict["blocked"] is True


def test_gate_registers_unknown_skill_instead_of_blocking_everything(tmp_path):
    """升级当天：受跟踪且本地干净的老技能必须照常自动更新，不能被"没登记"拦一轮。"""
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY)
    up = make_skill(str(tmp_path / "up"), "s1", body="第二版文档。\n")
    meta = {"name": "s1", "commit": "old", "content_hash": skills.dir_hash(live),
            "upstream_hash": "stale", "local_diff": False}
    verdict = capability.gate(cfg, "s1", live, up, meta=meta)
    assert verdict["allow"] is True and verdict["state"] == "active"
    assert capability.state_of(cfg, "s1") == "active"     # 顺手登记下来了


# ------------------------------------------------------------------ 与自动更新串起来

def _clean_meta(live):
    return {"name": "s1", "commit": "old", "content_hash": skills.dir_hash(live),
            "upstream_hash": "stale", "local_diff": False}


def test_update_stages_instead_of_overwriting_when_gate_blocks(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY, tracked=True)
    up = make_skill(str(tmp_path / "up"), "s1", body=SHELL_BODY)
    capability.ensure(cfg, log=lambda *_: None)
    meta = _clean_meta(live)
    action, why = update.update_one(cfg, "s1", live, meta, skills.meta_path(live), up, "newsha")
    assert action == "gated" and "只暂存待批" in why
    assert "不碰系统" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()
    assert any(p.startswith("s1-") for p in os.listdir(cfg.state_dir / "patrol" / "_pending"))


def test_update_still_auto_updates_clean_skill_without_new_risk(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY, tracked=True)
    up = make_skill(str(tmp_path / "up"), "s1", body="第二版文档，还是不动系统。\n")
    capability.ensure(cfg, log=lambda *_: None)
    meta = _clean_meta(live)
    action, _ = update.update_one(cfg, "s1", live, meta, skills.meta_path(live), up, "newsha")
    assert action == "updated"
    assert "第二版文档" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()


def test_accept_promotes_lifecycle_to_approved(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY, tracked=True)
    up = make_skill(str(tmp_path / "up"), "s1", body=SHELL_BODY)
    capability.ensure(cfg, log=lambda *_: None)
    capability.set_state(cfg, "s1", "candidate", why="上游新版待批", log=lambda *_: None)
    meta = _clean_meta(live)
    action, _ = update.update_one(cfg, "s1", live, meta, skills.meta_path(live), up, "newsha")
    assert action == "gated"
    result = update.accept(cfg, name="s1", allow_risky=True, log=lambda *_: None)
    assert result["accepted"] == ["s1"]
    assert capability.state_of(cfg, "s1") == "approved"
    assert "echo hello" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()


def test_overview_and_render_show_undeclared_risk(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    make_skill(str(tmp_path / "live"), "s1", body=SHELL_BODY)
    capability.ensure(cfg, log=lambda *_: None)
    data = capability.overview(cfg)
    row = data["skills"][0]
    assert row["state"] == "discovered" and row["undeclared_high_risk"] == ["shell"]
    text = capability.render(cfg)
    assert "未声明高危" in text and "只有 approved / active 允许自动更新" in text
    assert "证据分级" in text
    assert json.dumps(data, ensure_ascii=False)     # 可序列化（--json 用）


# ------------------------------------------------------------------ 证据分级（只认"要求执行"）

EXAMPLE_ONLY = "看图就懂：\n\n例如：\n\n```bash\ncurl https://example.invalid/x\n```\n"


def test_example_only_evidence_is_not_a_risk(tmp_path):
    """示例里的 shell/网络不该触发拦截 —— 第一版把 `>` 和行内反引号当信号，全是误报。"""
    info = capability.compare(make_skill(str(tmp_path / "live"), "s1", body=EXAMPLE_ONLY))
    assert info["levels"].get("network") == "example"
    assert info["undeclared_high_risk"] == []
    assert "network" in info["example_only_high_risk"]


def test_instructional_evidence_is_a_risk(tmp_path):
    instr = "请执行下面的命令完成安装：\n\n```bash\ncurl https://example.invalid/x -o y\n```\n"
    info = capability.compare(make_skill(str(tmp_path / "live"), "s1", body=instr))
    assert info["levels"].get("network") == "instructional"
    assert "network" in info["undeclared_high_risk"]


def test_example_only_upstream_does_not_block_update(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY, tracked=True)
    up = make_skill(str(tmp_path / "up"), "s1", body=EXAMPLE_ONLY)
    capability.ensure(cfg, log=lambda *_: None)
    assert capability.gate(cfg, "s1", live, up)["allow"] is True
    assert capability.new_high_risk(live, up, capability.read_declaration(live)) == []


# ------------------------------------------------------------------ 批准绑定内容 / 能力的来与去

def test_approval_is_bound_to_content_hash(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY, tracked=True)
    meta = {"name": "s1", "content_hash": skills.dir_hash(live), "local_diff": False}
    entry = capability.record_approval(cfg, "s1", live, meta=meta, log=lambda *_: None)
    assert entry["state"] == "approved" and entry["baseline_hash"] == meta["content_hash"]
    assert capability.gate(cfg, "s1", live, None, meta=meta)["allow"] is True
    make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY + "又加了一段\n", tracked=True)
    changed = {"name": "s1", "content_hash": skills.dir_hash(live), "local_diff": False}
    verdict = capability.gate(cfg, "s1", live, None, meta=changed)
    assert verdict["allow"] is False and "批准" in verdict["reasons"][0]


def test_sensitive_capability_coming_back_needs_fresh_approval(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    with_shell = make_skill(str(tmp_path / "live"), "s1", body=SHELL_BODY, tracked=True)
    capability.record_approval(cfg, "s1", with_shell,
                               meta={"content_hash": skills.dir_hash(with_shell)},
                               log=lambda *_: None)
    assert "shell" in capability.load(cfg)["skills"]["s1"]["capability_history"]
    # 把 shell 去掉后再批准：历史里仍留着 shell，但当前批准集合里没有
    without = make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY, tracked=True)
    capability.record_approval(cfg, "s1", without,
                               meta={"content_hash": skills.dir_hash(without)},
                               log=lambda *_: None)
    entry = capability.load(cfg)["skills"]["s1"]
    assert entry["approved_capabilities"] == [] and "shell" in entry["capability_history"]
    # shell 又回来：即使"本次 diff 没有新增"（不给 up_dir），也必须拦
    back = make_skill(str(tmp_path / "live"), "s1", body=SHELL_BODY, tracked=True)
    verdict = capability.gate(cfg, "s1", back, None,
                              meta={"content_hash": skills.dir_hash(back)})
    assert verdict["allow"] is False
    assert any("重新出现" in reason for reason in verdict["reasons"])


# ------------------------------------------------------------------ accept 要带证据

def _staged_pair(cfg, tmp_path, body_up=SHELL_BODY):
    live = make_skill(str(tmp_path / "live"), "s1", body=PLAIN_BODY)
    up = make_skill(str(tmp_path / "up"), "s1", body=body_up)
    skills.write_meta(live, {"name": "s1", "repo": "o/r", "local_diff": True,
                             "content_hash": skills.dir_hash(live)})
    skills.stage_skill(cfg, up, "s1", "abc1234")
    return live


def test_accept_refuses_when_staged_content_changed_after_review(tmp_path):
    """审的是 A、暂存的是 B → 拒绝（否则人在"以为审过"的情况下批准没审过的内容）。"""
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = _staged_pair(cfg, tmp_path)
    staged = skills.pending_items(cfg)[0]["path"]
    capability.mark_reviewed(cfg, "s1", staged)
    with open(os.path.join(staged, "SKILL.md"), "a", encoding="utf-8") as f:
        f.write("\n上游又改了一行\n")
    result = update.accept(cfg, name="s1", log=lambda *_: None)
    assert result["accepted"] == [] and "又变了" in result["skipped"]["s1"]
    assert "不碰系统" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()


def test_accept_requires_yes_for_new_high_risk(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = _staged_pair(cfg, tmp_path)
    blocked = update.accept(cfg, name="s1", log=lambda *_: None)
    assert blocked["accepted"] == [] and "shell" in " ".join(blocked["risky"]["s1"])
    assert "不碰系统" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()
    ok = update.accept(cfg, name="s1", allow_risky=True, log=lambda *_: None)
    assert ok["accepted"] == ["s1"]
    assert "```bash" in open(os.path.join(live, "SKILL.md"), encoding="utf-8").read()
    entry = capability.load(cfg)["skills"]["s1"]
    assert entry["state"] == "approved" and entry["baseline_hash"]
    assert "shell" in entry["approved_capabilities"]


def test_accept_risk_lists_capabilities_and_hints_state(tmp_path):
    cfg = make_cfg(tmp_path, [tmp_path / "live"])
    live = _staged_pair(cfg, tmp_path, body_up=EXAMPLE_ONLY)
    staged = skills.pending_items(cfg)[0]["path"]
    risk = capability.accept_risk(cfg, "s1", live, staged)
    assert risk["reasons"] == []                      # 只有示例级证据 → 不拦
    assert any("等级变化" in line for line in risk["lines"])
    assert any("不拦" in line for line in risk["lines"])
