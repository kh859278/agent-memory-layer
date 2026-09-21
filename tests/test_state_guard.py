"""`state/` 防护的测试：清单 / 快照 / "丢没丢"的判据（全离线、真临时目录）。

要钉死的口径：
  · **新装机器上不报"缺失"**（清单里的文件本来就不存在）—— 只有"历史快照里有过、现在没了"
    才是"被删了"的确切信号（2026-09-18 整棵 state/ 被删掉重建过，这条就是为它写的）
  · 快照写到 `backups/`（不在共享的 state 里），并按 keep 保留最近几份
  · 没有可备份的文件时**不报错**（新装机器跑巡检不该红）
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import config as cfgmod  # noqa: E402
from aml import state_guard  # noqa: E402


def make_cfg(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["skill_roots"] = []
    return cfg


def write_state(cfg, rel, body="x"):
    path = cfg.state_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def noop(*_a, **_k):
    pass


# ------------------------------------------------------------------ 清单与说明

def test_durable_manifest_is_non_empty_and_named():
    entries = state_guard.entries()
    assert entries and all(e["path"] and e["why"] for e in entries)
    paths = {e["path"] for e in entries}
    assert "recall-log.jsonl" in paths and "patrol/lifecycle.json" in paths


def test_readme_says_state_is_not_a_cache(tmp_path):
    cfg = make_cfg(tmp_path)
    path = state_guard.write_readme(cfg)
    text = open(path, encoding="utf-8").read()
    assert "不是缓存" in text and "recall-log.jsonl" in text
    assert "aml state snapshot" in text
    assert os.path.dirname(path) == str(cfg.state_dir)


# ------------------------------------------------------------------ 快照

def test_snapshot_copies_durable_files(tmp_path):
    cfg = make_cfg(tmp_path)
    write_state(cfg, "recall-log.jsonl", "事件")
    write_state(cfg, "patrol/lifecycle.json", "{}")
    info = state_guard.snapshot(cfg, stamp="20260101-000000", log=noop)
    assert sorted(info["copied"]) == ["patrol/lifecycle.json", "recall-log.jsonl"]
    assert (cfg.backups_dir / "state" / "20260101-000000" / "recall-log.jsonl").is_file()
    assert "bench-tasks.jsonl" in info["missing"]


def test_snapshot_without_files_is_not_an_error(tmp_path):
    cfg = make_cfg(tmp_path)
    info = state_guard.snapshot(cfg, log=noop)
    assert info["copied"] == [] and info["dir"] is None
    assert state_guard.snapshots(cfg) == []


def test_snapshot_prunes_old_ones_and_never_prunes_itself(tmp_path):
    cfg = make_cfg(tmp_path)
    write_state(cfg, "recall-log.jsonl", "事件")
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        state_guard.snapshot(cfg, keep=2, stamp=stamp, log=noop)
    names = [p.name for p in state_guard.snapshots(cfg)]
    assert names == ["20260103-000000", "20260102-000000"]      # 新→旧，只留 2 份
    # keep=0 不能把刚打的那份也清掉
    info = state_guard.snapshot(cfg, keep=0, stamp="20260104-000000", log=noop)
    assert (cfg.backups_dir / "state" / "20260104-000000").is_dir()
    assert info["pruned"] == [] or "20260104-000000" not in info["pruned"]


# ------------------------------------------------------------------ 丢没丢

def test_fresh_install_reports_nothing(tmp_path):
    cfg = make_cfg(tmp_path)
    assert state_guard.findings(cfg) == []
    assert "没有发现丢失或过期" in state_guard.render_findings([])


def test_loss_after_snapshot_is_reported_as_bad(tmp_path):
    cfg = make_cfg(tmp_path)
    write_state(cfg, "recall-log.jsonl", "事件")
    state_guard.snapshot(cfg, stamp="20260101-000000", log=noop)
    (cfg.state_dir / "recall-log.jsonl").unlink()          # 模拟"被删了"
    findings = state_guard.findings(cfg)
    assert len(findings) == 1
    assert findings[0]["level"] == "bad" and findings[0]["path"] == "recall-log.jsonl"
    assert "不见了" in findings[0]["detail"]
    assert "backups" in findings[0]["fix"]
    assert "❌" in state_guard.render_findings(findings)


def test_never_snapshotted_file_is_not_reported(tmp_path):
    """清单里有、但从来没生成过 → 不报（新装机器不该一片红）。"""
    cfg = make_cfg(tmp_path)
    write_state(cfg, "patrol/lifecycle.json", "{}")
    state_guard.snapshot(cfg, stamp="20260101-000000", log=noop)
    assert [f["path"] for f in state_guard.findings(cfg)] == []


def test_stale_file_is_warned(tmp_path):
    cfg = make_cfg(tmp_path)
    path = write_state(cfg, "recall-log.jsonl", "事件")
    state_guard.snapshot(cfg, stamp="20260101-000000", log=noop)
    old = time.time() - 60 * 86400
    os.utime(path, (old, old))
    findings = state_guard.findings(cfg)
    assert findings and findings[0]["level"] == "warn" and "没更新" in findings[0]["detail"]


def test_summarize_counts_present_and_snapshots(tmp_path):
    cfg = make_cfg(tmp_path)
    assert state_guard.summarize(cfg)["present"] == 0
    write_state(cfg, "recall-log.jsonl", "事件")
    state_guard.snapshot(cfg, stamp="20260101-000000", log=noop)
    info = state_guard.summarize(cfg)
    assert info["present"] == 1 and info["total"] == len(state_guard.entries())
    assert info["snapshots"] == 1


# ------------------------------------------------------------------ 接进巡检

def test_patrol_run_snapshots_state_and_writes_readme(tmp_path, monkeypatch):
    """巡检必须带上这一步 —— 否则"下次整棵 state 被删"还是没人知道。

    其余阶段全部替换成假实现（不联网、不动技能），只验证 5/5 真的跑了。
    """
    import types

    from aml import patrol_cli
    from aml.patrol import capability

    cfg = make_cfg(tmp_path)
    write_state(cfg, "recall-log.jsonl", "事件")
    monkeypatch.setattr(patrol_cli.update, "adopt", lambda *a, **k: {})
    monkeypatch.setattr(patrol_cli, "cmd_patrol_update", lambda *a, **k: 0)
    monkeypatch.setattr(patrol_cli, "cmd_patrol_packages", lambda *a, **k: 0)
    monkeypatch.setattr(patrol_cli.skills, "mirror", lambda *a, **k: {})
    monkeypatch.setattr(patrol_cli.skills, "write_inventory", lambda *a, **k: "清单")
    monkeypatch.setattr(capability, "ensure", lambda *a, **k: {})
    monkeypatch.setattr(patrol_cli, "NoticeQueue",
                        lambda *a, **k: types.SimpleNamespace(brief=lambda *a, **k: "",
                                                             add=lambda *a, **k: None))
    args = types.SimpleNamespace(no_adopt=True, no_sync=True, no_notify=True, deep=False)
    logs = []
    assert patrol_cli.cmd_patrol_run(cfg, args, log=logs.append) == 0
    assert any("5/5" in line for line in logs)
    assert (cfg.state_dir / state_guard.README_NAME).is_file()
    copies = [p.name for p in (cfg.backups_dir / "state").iterdir()]
    assert copies, "快照目录没建起来"
    assert (cfg.backups_dir / "state" / copies[0] / "recall-log.jsonl").is_file()
