"""任务级基准的测试：**全离线**（stub agent，不联网、不起真 agent、不花 token）。

要钉死的口径：
  · prompt 走 stdin、stdout 出 JSON；崩了/输出不能解析 → 记成错误，不是抛异常
  · `_hidden/` 默认不给 agent，判分前才拷进去（这是唯一能区分"记得"与"猜得到"的手段）
  · success = 验收退出码 0 且 expect 都在、forbidden 都不在
  · regression 只在"起跑前能过、跑完挂了"时才算账
  · 自动反馈的归因只看**这条记忆自己的正文**，不搞连坐
"""
from __future__ import annotations

import json
import sys
import types

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

import pytest  # noqa: E402

from aml import taskbench  # noqa: E402

STUB_SRC = r'''
import json
import sys

prompt = sys.stdin.read()
mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
if mode == "fail":
    sys.stderr.write("stub: 我崩了\n")
    raise SystemExit(3)
if mode == "garbage":
    print("这不是 JSON")
    raise SystemExit(0)
if mode == "write":
    with open("made.txt", "w", encoding="utf-8") as f:
        f.write("hello")
if mode == "advice":
    with open("made.txt", "w", encoding="utf-8") as f:
        f.write("用了 write_text(newline=...) 的写法")
if mode == "break":
    with open("gate.txt", "w", encoding="utf-8") as f:
        f.write("bad")
print(json.dumps({"result": "done:" + prompt[:12], "num_turns": 2, "duration_ms": 120,
                  "total_cost_usd": 0.01,
                  "usage": {"input_tokens": 10, "output_tokens": 5}}))
'''


@pytest.fixture
def stub(tmp_path):
    path = tmp_path / "stub_agent.py"
    path.write_text(STUB_SRC, encoding="utf-8")
    return path


def agent_argv(stub, mode="ok"):
    return [sys.executable, str(stub), mode]


def make_task(**over):
    task = {"id": "t1", "prompt": "做点事", "fixture": "demo",
            "verify": [sys.executable, "-c", "raise SystemExit(0)"]}
    task.update(over)
    return task


def make_fixture(root, name="demo", files=None, hidden=None):
    base = root / name
    (base / "_hidden").mkdir(parents=True, exist_ok=True)
    for rel, body in (files or {"gate.txt": "ok"}).items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    for rel, body in (hidden or {}).items():
        target = base / "_hidden" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return base


# ------------------------------------------------------------------ 任务表与 prompt

def test_load_tasks_requires_prompt(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"id": "a"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        taskbench.load_tasks(str(path))


def test_load_tasks_fills_defaults_and_accepts_string_commands(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"id": "a", "prompt": "p", "verify": "python -m pytest -q"},
                               ensure_ascii=False), encoding="utf-8")
    task = taskbench.load_tasks(str(path))[0]
    assert task["phase"] == "P2" and task["expect"] == [] and task["timeout"] > 0
    assert task["verify"][:2] == ["python", "-m"]


def test_build_prompt_disables_memory_when_off():
    task = {"prompt": "干活"}
    off = taskbench.build_prompt(task, "")
    on = taskbench.build_prompt(task, "[沉淀|win|2026-01-01|0.9] 别用 GBK 直接 print")
    assert taskbench.NO_MEMORY_RULE in off and "记忆层" not in off
    assert "别用 GBK" in on and "记忆层" in on


# ------------------------------------------------------------------ 跑 agent

def test_run_agent_parses_json_fields(stub, tmp_path):
    run = taskbench.run_agent(agent_argv(stub), "你好", str(tmp_path))
    assert run["ok"] is True and run["turns"] == 2
    assert (run["tokens_in"], run["tokens_out"]) == (10, 5)
    assert run["cost_usd"] == 0.01 and run["result_text"].startswith("done:")


def test_run_agent_records_failures_instead_of_raising(stub, tmp_path):
    bad = taskbench.run_agent(agent_argv(stub, "fail"), "x", str(tmp_path))
    assert bad["ok"] is False and "退出码 3" in bad["error"]
    junk = taskbench.run_agent(agent_argv(stub, "garbage"), "x", str(tmp_path))
    assert junk["ok"] is False and junk["error"] and junk["turns"] is None
    missing = taskbench.run_agent([str(tmp_path / "nope.exe")], "x", str(tmp_path))
    assert missing["ok"] is False and missing["error"]


def test_run_agent_times_out_without_raising(stub, tmp_path):
    run = taskbench.run_agent(
        [sys.executable, "-c", "import time; time.sleep(30)"], "x", str(tmp_path), timeout=1)
    assert run["ok"] is False and "超时" in run["error"]


# ------------------------------------------------------------------ 工作区与 _hidden

def test_hidden_files_are_withheld_then_injected(tmp_path):
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures, hidden={"check.py": "print('ok')"})
    work = tmp_path / "work"
    taskbench.prepare_workspace({"fixture": "demo"}, str(work), str(fixtures))
    assert (work / "gate.txt").is_file() and not (work / "_hidden").exists()
    copied = taskbench.inject_hidden({"fixture": "demo"}, str(work), str(fixtures))
    assert copied == ["check.py"] and (work / "check.py").is_file()


def test_changed_files_detects_add_modify_delete(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    (root / "keep.txt").write_text("a", encoding="utf-8")
    (root / "gone.txt").write_text("b", encoding="utf-8")
    before = taskbench.snapshot(str(root))
    (root / "keep.txt").write_text("a2", encoding="utf-8")
    (root / "new.txt").write_text("c", encoding="utf-8")
    (root / "gone.txt").unlink()
    after = taskbench.snapshot(str(root))
    assert taskbench.changed_files(before, after) == ["gone.txt", "keep.txt", "new.txt"]


# ------------------------------------------------------------------ 判分与执行

def test_execute_grades_success_and_lists_changes(tmp_path, stub):
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    verify = [sys.executable, "-c",
              "import sys; sys.exit(0 if open('made.txt').read() == 'hello' else 1)"]
    task = make_task(verify=verify, expect=["hello"])
    row = taskbench.execute(None, task, "off", agent_argv(stub, "write"),
                            fixtures=str(fixtures), log=lambda *a: None)
    assert row["success"] is True and row["changed_files"] == ["made.txt"]
    assert row["injected_chars"] == 0 and row["turns"] == 2


def test_execute_flags_forbidden_advice(tmp_path, stub):
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    task = make_task(verify=[sys.executable, "-c", "raise SystemExit(0)"],
                     forbidden=["write_text(newline"])
    row = taskbench.execute(None, task, "off", agent_argv(stub, "advice"),
                            fixtures=str(fixtures), log=lambda *a: None)
    assert row["forbidden_hits"] == ["write_text(newline"] and row["success"] is False


def test_execute_only_counts_regression_when_baseline_passed(tmp_path, stub):
    gate = [sys.executable, "-c",
            "import sys; sys.exit(0 if open('gate.txt').read() == 'ok' else 1)"]
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    task = make_task(verify=gate, regression=gate)
    row = taskbench.execute(None, task, "off", agent_argv(stub, "break"),
                            fixtures=str(fixtures), log=lambda *a: None)
    assert row["baseline_rc"] == 0 and row["verify_rc"] == 1 and row["regressed"] is True

    broken = tmp_path / "broken"
    make_fixture(broken, files={"gate.txt": "bad"})
    row2 = taskbench.execute(None, make_task(verify=gate, regression=gate), "off",
                             agent_argv(stub), fixtures=str(broken), log=lambda *a: None)
    assert row2["baseline_rc"] == 1 and row2["regressed"] is False   # 起跑就挂 → 不算账


def test_execute_injects_hidden_before_grading(tmp_path, stub):
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures, hidden={"check.py": "import sys; sys.exit(0)"})
    task = make_task(verify=[sys.executable, "check.py"])
    row = taskbench.execute(None, task, "off", agent_argv(stub), fixtures=str(fixtures),
                            log=lambda *a: None)
    assert row["hidden_files"] == ["check.py"] and row["verify_rc"] == 0
    assert row["changed_files"] == []      # 判分用的文件不该算成 agent 的改动


# ---------------------------------------------------- 反事实臂（2026-09-21）

def _three_items_retrieve(cfg, task, log=None):
    items = [{"hash": f"h{i}", "text": f"line{i}", "chars": 10} for i in (1, 2, 3)]
    return {"text": "\n".join(i["text"] for i in items), "hashes": [i["hash"] for i in items],
            "items": items, "lines": 3, "chars": 30, "empty": False, "diag": {}}


def test_ablate_arm_hides_the_top_memories(tmp_path, stub, monkeypatch):
    """反事实臂：藏掉排在最前的 N 条 —— 用来回答"注入的记忆到底有没有被用上"。"""
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    monkeypatch.setattr(taskbench, "retrieve", _three_items_retrieve)
    tasks = [make_task(verify=[sys.executable, "-c", "raise SystemExit(0)"], expect=["done"],
                       query="q")]
    report = taskbench.evaluate(None, tasks, arms=["on"], agent=agent_argv(stub),
                                fixtures=str(fixtures), log=lambda *a: None, ablate=1)
    assert "ablate" in report["arms"]                       # 自动加上的
    on_row = next(r for r in report["rows"] if r["arm"] == "on")
    ab_row = next(r for r in report["rows"] if r["arm"] == "ablate")
    assert on_row["injected_chars"] == 30 and ab_row["injected_chars"] == 20
    assert ab_row["ablated_hashes"] == ["h1"] and ab_row["injected_hashes"] == ["h2", "h3"]
    assert report["ablate_delta"] is not None
    assert "反事实" in taskbench.render(report)


def test_ablate_zero_keeps_two_arms(tmp_path, stub, monkeypatch):
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    monkeypatch.setattr(taskbench, "retrieve", _three_items_retrieve)
    report = taskbench.evaluate(None, [make_task(query="q")], arms=["off", "on"],
                                agent=agent_argv(stub), fixtures=str(fixtures),
                                log=lambda *a: None, ablate=0)
    assert set(report["arms"]) == {"off", "on"} and report["ablate_delta"] is None


def test_off_arm_still_never_claims_memory_use(tmp_path, stub):
    """OFF 组与反事实组之外的组不许报"用了注入的经验"（历史 bug 的回归测试）。"""
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    task = make_task(verify=[sys.executable, "-c", "raise SystemExit(0)"],
                     expect_memory=["hello"])
    row = taskbench.execute(None, task, "off", agent_argv(stub, "write"),
                            fixtures=str(fixtures), log=lambda *a: None)
    assert row["memory_used"] == []


def test_grading_ignores_hidden_file_text(tmp_path, stub, monkeypatch):
    """验收标准自己的正文不能进判分探头：它里面往往就写着"正确做法"。

    第一版把 `_hidden/` 注入后才扫工作区，于是 `expect_memory` 永远命中、
    `forbidden` 可能被验收文件自己触发 —— 指标全假。
    """
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures, hidden={"check.py": "# 正确做法：encoding=\"utf-8\"\n"})
    monkeypatch.setattr(taskbench, "retrieve", _fake_retrieve)
    task = make_task(verify=[sys.executable, "-c", "raise SystemExit(0)"],
                     forbidden=["encoding=\"utf-8\""], expect_memory=["encoding=\"utf-8\""])
    row = taskbench.execute(None, task, "on", agent_argv(stub), fixtures=str(fixtures),
                            log=lambda *a: None)
    assert row["forbidden_hits"] == [] and row["memory_used"] == []


def test_off_arm_never_claims_memory_was_used(tmp_path, stub):
    """OFF 组没有注入，工作区里出现同名串纯属巧合，不能算成"用了注入的经验"。"""
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures, files={"note.txt": "这里出现了 split 这个词"})
    task = make_task(verify=[sys.executable, "-c", "raise SystemExit(0)"], expect_memory=["split"])
    row = taskbench.execute(None, task, "off", agent_argv(stub), fixtures=str(fixtures),
                            log=lambda *a: None)
    assert row["memory_used"] == []


# ------------------------------------------------------------------ 汇总与渲染

def _fake_retrieve(cfg, task, log=None):
    return {"text": "line", "hashes": ["h1"], "items": [{"hash": "h1", "text": "line", "chars": 4}],
            "lines": 1, "chars": 4, "empty": False, "diag": {}}


def test_evaluate_compares_arms_and_reports_delta(tmp_path, stub, monkeypatch):
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures, files={"gate.txt": "ok"})
    monkeypatch.setattr(taskbench, "retrieve", _fake_retrieve)
    task = make_task(verify=[sys.executable, "-c", "raise SystemExit(0)"], expect=["done"],
                     query="q")
    report = taskbench.evaluate(None, [task], arms=["off", "on"], agent=agent_argv(stub),
                                fixtures=str(fixtures), log=lambda *a: None)
    assert report["arms"]["off"]["injected_chars_avg"] == 0
    assert report["arms"]["on"]["injected_chars_avg"] == 4
    assert report["arms"]["on"]["success_rate"] == report["arms"]["off"]["success_rate"] == 1.0
    assert report["delta"]["success_rate"] == 0.0
    text = taskbench.render(report)
    assert "ON − OFF" in text and "只看 ON − OFF" in text and "成功率" in text


def test_render_marks_runs_that_need_eyes(tmp_path, stub):
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures, files={"gate.txt": "ok"})
    task = make_task(verify=[sys.executable, "-c", "raise SystemExit(0)"],
                     forbidden=["hello"])
    row = taskbench.execute(None, task, "off", agent_argv(stub, "write"),
                            fixtures=str(fixtures), log=lambda *a: None)
    report = taskbench.summarize([row])
    assert "t1/off" in taskbench.render(report)


def test_save_report_drops_injected_bodies(tmp_path):
    cfg = types.SimpleNamespace(state_dir=tmp_path)
    report = {"tasks": 1, "runs": 1, "arms": {}, "delta": None,
              "rows": [{"id": "t1", "memory_items": [{"hash": "h", "text": "秘密正文"}]}]}
    path = taskbench.save_report(cfg, report, when="20260101-000000")
    saved = json.loads(open(path, encoding="utf-8").read())
    assert "memory_items" not in saved["rows"][0] and "秘密正文" not in json.dumps(saved)


# ------------------------------------------------------------------ 自动反馈归因

def test_post_feedback_attributes_only_with_evidence(monkeypatch, tmp_path):
    calls = []

    def fake_record(cfg, content_hash, outcome, note="", log=print):
        calls.append((content_hash, outcome, note))
        return {"ok": True}

    from aml import feedback
    monkeypatch.setattr(feedback, "record", fake_record)
    row = {"id": "t1", "arm": "on", "success": True,
           "forbidden_hits": ["write_text(newline"],
           "memory_used": ["write_lf"],
           "memory_items": [{"hash": "h-bad", "text": "用 write_text(newline=...) 就行"},
                            {"hash": "h-good", "text": "用 text.write_lf() 写文件"},
                            {"hash": "h-meh", "text": "无关的别的东西"}]}
    info = taskbench.post_feedback(None, row, log=lambda *a: None)
    verdicts = dict((h, o) for h, o, _ in calls)
    assert verdicts == {"h-bad": "failed", "h-good": "worked", "h-meh": "used"}
    assert info["recorded"] == 3
    assert all(note.startswith("taskbench:t1:on") for _, _, note in calls)


def test_post_feedback_never_claims_worked_without_a_mark(monkeypatch):
    calls = []
    from aml import feedback
    monkeypatch.setattr(feedback, "record",
                        lambda cfg, h, outcome, note="", log=print: calls.append((h, outcome))
                        or {"ok": True})
    row = {"id": "t1", "arm": "on", "success": True, "forbidden_hits": [],
           "memory_used": ["出现在别处的串"],
           "memory_items": [{"hash": "h1", "text": "这条正文里没有那个串"}]}
    taskbench.post_feedback(None, row, log=lambda *a: None)
    assert calls == [("h1", "used")]


def test_post_feedback_skips_when_nothing_injected():
    assert taskbench.post_feedback(None, {"id": "t", "arm": "on", "memory_items": []},
                                   log=lambda *a: None) == {"recorded": 0}
