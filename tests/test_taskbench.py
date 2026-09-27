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
if mode == "flaky":
    import os
    n = int(open(".n").read()) if os.path.exists(".n") else 0
    n += 1
    open(".n", "w").write(str(n))
    with open(".seen", "a", encoding="utf-8") as f:
        f.write(("hidden" if os.path.exists("check.py") else "clean") + "|"
                + ("fb" if "验收没通过" in prompt else "fresh") + "\n")
    if n >= 3:
        with open("made.txt", "w", encoding="utf-8") as f:
            f.write("done")
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


# ------------------------------------------------------------------ 重试与纠正次数

def flaky_stub(tmp_path):
    path = tmp_path / "stub_agent.py"
    path.write_text(STUB_SRC, encoding="utf-8")
    return path


def _verifies(path_name):
    return [sys.executable, "-c",
            f"import os,sys; sys.exit(0 if os.path.exists({path_name!r}) else 1)"]


def test_load_tasks_defaults_max_rounds(tmp_path):
    """默认只跑一次：老任务表的行为不能被改掉。"""
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"id": "a", "prompt": "p"}), encoding="utf-8")
    assert taskbench.load_tasks(str(path))[0]["max_rounds"] == 1


def test_remove_hidden_cleans_workspace_but_keeps_root(tmp_path):
    work = tmp_path / "w"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "a.py").write_text("x", encoding="utf-8")
    (work / "top.py").write_text("y", encoding="utf-8")
    taskbench._remove_hidden(str(work), ["sub/a.py", "top.py", "根本没有这个文件"])
    assert not (work / "sub").exists() and not (work / "top.py").exists()
    assert work.is_dir(), "工作区本身不能被删掉"


def test_retry_feeds_output_back_and_counts_corrections(tmp_path):
    """第三轮才做对 → 纠正次数 2；且每轮开始前隐藏验收都得是撤走的。"""
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures, hidden={"check.py": "print('hidden')"})
    stub = flaky_stub(tmp_path)
    work = tmp_path / "work"
    task = make_task(max_rounds=5, verify=_verifies("made.txt"))
    row = taskbench.execute(None, task, "off", agent_argv(stub, "flaky"),
                            fixtures=str(fixtures), workdir=str(work), keep=True)
    assert row["success"] is True
    assert row["attempts"] == 3, row["attempts_detail"]
    assert row["corrections"] == 2
    seen = (work / ".seen").read_text(encoding="utf-8").split()
    assert seen == ["clean|fresh", "clean|fb", "clean|fb"], seen


def test_retry_stops_at_cap_when_never_solved(tmp_path):
    """始终没做对 → 纠正次数 = 跑了几轮（每轮都错），且 success 为假。"""
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    stub = flaky_stub(tmp_path)
    task = make_task(max_rounds=3, verify=_verifies("never.txt"))
    row = taskbench.execute(None, task, "off", agent_argv(stub, "flaky"),
                            fixtures=str(fixtures))
    assert row["success"] is False
    assert row["attempts"] == 3 and row["corrections"] == 3


def test_default_single_round_keeps_old_behaviour(tmp_path):
    """没写 max_rounds 时：只跑一次，失败就是失败，纠正次数记 1。"""
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    stub = flaky_stub(tmp_path)
    task = make_task(verify=_verifies("made.txt"))
    row = taskbench.execute(None, task, "off", agent_argv(stub, "flaky"),
                            fixtures=str(fixtures))
    assert row["attempts"] == 1 and row["success"] is False
    assert row["corrections"] == 1 and row["max_rounds"] == 1


def test_retry_sums_cost_and_tokens_across_attempts(tmp_path):
    """多轮的成本/token 要累加，不是只记最后一次。"""
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    stub = flaky_stub(tmp_path)
    task = make_task(max_rounds=5, verify=_verifies("made.txt"))
    row = taskbench.execute(None, task, "off", agent_argv(stub, "flaky"),
                            fixtures=str(fixtures))
    assert row["attempts"] == 3
    assert row["cost_usd"] == pytest.approx(0.03)      # 每次 0.01
    assert row["tokens_in"] == 30 and row["tokens_out"] == 15
    assert row["turns"] == 6                            # 每次 2 轮


# --------------------------------------------------- 部分分（判分器 SUMMARY）
#
# 为什么补这组：判分器早就在打 `SUMMARY {core_pass, core_total, ...}`，
# 但基准一直只记退出码（二值）。2026-09-24 三组配套实测里 OFF 核心 0/7、2/7、0/7，
# ON 核心 7/7、6/7、7/7 —— 差 6 项的核心判据，在二值上只剩"差 1 组"。
# 丢掉分辨率的不是记忆层，是指标层。

def test_parse_summary_takes_the_last_one():
    out = ('[PASS] a\nSUMMARY {"core_pass": 1, "core_total": 2}\n'
           '中间噪音\nSUMMARY {"core_pass": 2, "core_total": 2}\n')
    assert taskbench.parse_summary(out) == {"core_pass": 2, "core_total": 2}


def test_parse_summary_is_empty_without_summary():
    """老判分器没有 SUMMARY：不能因此把整轮作废。"""
    assert taskbench.parse_summary("[PASS] a\n[FAIL] b\n") == {}
    assert taskbench.parse_summary("SUMMARY 这不是 JSON") == {}
    assert taskbench.parse_summary("") == {}


def test_checks_from_computes_core_rate():
    out = 'SUMMARY {"core_pass": 6, "core_total": 7, "style_pass": 3, "style_total": 4}'
    checks = taskbench.checks_from({"output": out})
    assert checks["core_pass"] == 6 and checks["core_total"] == 7
    assert checks["core_rate"] == pytest.approx(0.857)
    # 只有 pass/total 的判分器也要算得出比率
    assert taskbench.checks_from({"output": 'SUMMARY {"pass": 1, "total": 4}'})["core_rate"] == 0.25
    # 全无 SUMMARY → 不编一个出来
    assert taskbench.checks_from({"output": "没有结构"}) == {}


def test_grade_carries_checks_from_verify_output():
    verify = {"rc": 1, "output": 'SUMMARY {"core_pass": 0, "core_total": 7}',
              "skipped": False}
    row = taskbench.grade({"id": "t"}, {}, ".", {}, {}, verify=verify, workspace_text="")
    assert row["checks"]["core_total"] == 7 and row["checks"]["core_rate"] == 0.0


def test_summarize_reports_core_rate_and_small_delta():
    """核心判据差值要保留三位小数 —— 0.857 不能被压成 0.9，更不能抹成 0。"""
    rows = []
    for arm, core in (("off", 0), ("on", 6)):
        rows.append({"id": "t", "arm": arm, "success": False, "corrections": 1,
                     "checks": {"core_pass": core, "core_total": 7, "core_rate": core / 7},
                     "changed_files": [], "wall_ms": 1, "tokens_in": 1, "tokens_out": 1,
                     "cost_usd": 0.0, "ok": True})
    rep = taskbench.summarize(rows, arms=["off", "on"])
    assert rep["arms"]["on"]["core_rate_avg"] == pytest.approx(0.857)
    assert rep["arms"]["on"]["core_total_total"] == 7
    assert rep["delta"]["core_rate"] == pytest.approx(0.857)
    assert rep["delta"]["success_rate"] == 0.0        # 二值上确实看不出差别
    assert "0.857" in taskbench.render(rep)


# --------------------------------------------------- 重试喂回去什么（泄题守卫）

def fails_with(text):
    """一个必定失败、且把 `text` 打出来的验收命令（模拟判分器点名规则）。"""
    return [sys.executable, "-c", f"print({text!r}); raise SystemExit(1)"]


REC_SRC = r'''
import json
import sys

prompt = sys.stdin.read()
with open(".prompts.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"prompt": prompt}, ensure_ascii=False) + "\n")
print(json.dumps({"result": "done", "num_turns": 1, "duration_ms": 10,
                  "total_cost_usd": 0.01,
                  "usage": {"input_tokens": 1, "output_tokens": 1}}))
'''


def rec_stub(tmp_path):
    path = tmp_path / "rec_agent.py"
    path.write_text(REC_SRC, encoding="utf-8")
    return path


def prompts_of(work):
    with open(work / ".prompts.jsonl", encoding="utf-8") as f:
        return [json.loads(line)["prompt"] for line in f if line.strip()]


def _run_feedback(tmp_path, feedback, **over):
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    work = tmp_path / ("work-" + feedback)
    # 声明泄题词，守卫才有东西可查（不声明 = 不设防，这是设计）
    over.setdefault("leak_words", ["0o700", "chmod"])
    task = make_task(max_rounds=3, fixture="demo", feedback=feedback,
                     verify=fails_with("需要 0o700，先 chmod"), **over)
    row = taskbench.execute(None, task, "off", agent_argv(rec_stub(tmp_path), ""),
                            fixtures=str(fixtures), workdir=str(work), keep=True)
    return row, prompts_of(work)


def test_feedback_hidden_feeds_grader_output(tmp_path):
    """默认档：把判分器的原话喂回去（老行为，但**它会泄题**）。"""
    row, prompts = _run_feedback(tmp_path, "hidden")
    assert len(prompts) == 3
    assert "0o700" in prompts[1] and "验收没通过" in prompts[1]
    assert row["feedback_mode"] == "hidden"
    assert row["feedback_leaks"] == ["0o700", "chmod"]   # 如实记账，不假装没泄
    assert row["feedback_log"][0]["leaks"] == ["0o700", "chmod"]
    assert row["feedback_log"][0]["source"] == "hidden"


def test_feedback_none_never_shows_grader_output(tmp_path):
    """none 档：只说"没过"，不给任何外部信息 —— 这才是"独立返工"。"""
    row, prompts = _run_feedback(tmp_path, "none")
    assert len(prompts) == 3
    assert "0o700" not in prompts[1] and "chmod" not in prompts[1]
    assert "验收没通过" in prompts[1]
    assert row["feedback_leaks"] == []
    assert [f["source"] for f in row["feedback_log"]] == ["none", "none"]


def test_feedback_public_feeds_only_the_public_check(tmp_path):
    """public 档：只喂工作区里本来就有、agent 自己也能跑的检查输出。"""
    row, prompts = _run_feedback(
        tmp_path, "public",
        public=[sys.executable, "-c", "print('公开检查：产物还不能用')"])
    assert "公开检查：产物还不能用" in prompts[1]
    assert "0o700" not in prompts[1]               # 隐藏判据一个字都没漏
    assert row["feedback_leaks"] == []
    assert row["feedback_log"][0]["source"] == "public"


def test_feedback_public_without_a_public_check_falls_back_to_none(tmp_path):
    row, prompts = _run_feedback(tmp_path, "public")
    assert "0o700" not in prompts[1]
    assert row["feedback_log"][0]["source"] == "none"
    assert row["feedback_log"][0]["chars"] == 0


def test_unknown_feedback_mode_falls_back_to_hidden(tmp_path):
    """任务表写错档位不许静默变成"不喂" —— 那会把返工能力测成运气。"""
    row, _ = _run_feedback(tmp_path, "乱写的")
    assert row["feedback_mode"] == "hidden"


def test_public_check_does_not_pollute_the_workspace(tmp_path):
    """公开检查自己写出来的垃圾不能算成 agent 的产出。"""
    fixtures = tmp_path / "fixtures"
    make_fixture(fixtures)
    work = tmp_path / "work"
    task = make_task(max_rounds=3, fixture="demo", feedback="public",
                     public=[sys.executable, "-c",
                             "open('公开检查留下的垃圾.txt', 'w').write('x')"],
                     verify=fails_with("失败"))
    taskbench.execute(None, task, "off", agent_argv(rec_stub(tmp_path), ""),
                      fixtures=str(fixtures), workdir=str(work), keep=True)
    assert not (work / "公开检查留下的垃圾.txt").exists()


# --------------------------------------------------- forbidden 查哪儿（自述还是产物）
#
# 为什么补这组：2026-09-26 实测，journal 组的 ON 交出了 7/7 全对的一篇，
# 收尾里写了一句「其他｜未写校内指导老师」，而 `forbidden` 是子串判据 ——
# **它声明自己没写，反被判成写了**，成功被抹成失败。头条的成功率因此低了一档。

def _grade_forbidden(scope, result_text, artifact_text):
    task = {"id": "t", "forbidden": ["校内指导老师"]}
    if scope is not None:
        task["forbidden_in"] = scope
    verify = {"rc": 0, "output": "", "skipped": False}
    return taskbench.grade(task, {"result_text": result_text}, ".", {}, {},
                           verify=verify, workspace_text=artifact_text)


def test_forbidden_default_looks_at_agent_narration():
    """默认档（老行为）：agent 自述里说了也算 —— 这正是那条假违规的来源。"""
    row = _grade_forbidden(None, "其他｜未写校内指导老师、未附参考资料", "正文很干净")
    assert row["forbidden_hits"] == ["校内指导老师"]
    assert row["forbidden_in"] == "probe"
    assert row["success"] is False


def test_forbidden_artifacts_scope_ignores_self_narration():
    """artifacts 档：只在产物里查 —— 声明自己没写就不该被判成写了。"""
    row = _grade_forbidden("artifacts", "其他｜未写校内指导老师、未附参考资料", "正文很干净")
    assert row["forbidden_hits"] == []
    assert row["forbidden_in"] == "artifacts"
    assert row["success"] is True


def test_forbidden_artifacts_scope_still_catches_the_deliverable():
    """但产物里真出现了，照样要抓 —— 这一档不是"放宽"，是"换个地方查"。"""
    row = _grade_forbidden("artifacts", "我写好了", "本篇由校内指导老师审阅")
    assert row["forbidden_hits"] == ["校内指导老师"]
    assert row["success"] is False


def test_unknown_forbidden_scope_falls_back_to_probe():
    row = _grade_forbidden("乱写的", "未写校内指导老师", "")
    assert row["forbidden_in"] == "probe"
    assert row["forbidden_hits"] == ["校内指导老师"]


def test_load_tasks_normalizes_forbidden_in(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"id": "a", "prompt": "p", "forbidden_in": "artifacts"}),
                    encoding="utf-8")
    assert taskbench.load_tasks(str(path))[0]["forbidden_in"] == "artifacts"
