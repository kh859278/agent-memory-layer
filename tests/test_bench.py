"""基准测试的测试：任务表解析、命中判定、聚合口径（假 retriever，不联网）。

口径要在测试里钉死，否则以后很容易被"调数字"：
  · 命中 = expect 里任一子串出现在召回文本里
  · `expect` 为空的行**不参与命中率**（只观察），否则会平白拉低数字
  · memory OFF 恒为 0 命中 / 0 字（它是基线，不是被测对象）
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import bench  # noqa: E402
from aml import config as cfgmod  # noqa: E402


class FakeResult:
    def __init__(self, lines, used=None):
        self.lines = lines
        self.used = used if used is not None else sum(len(x) for x in lines)
        self.diag = {}

    @property
    def empty(self):
        return not self.lines


class FakeRetriever:
    def __init__(self, mapping):
        self.mapping = mapping          # query -> lines
        self.calls = []

    def search(self, query, phase="P2", project=None, tag=None, n=None, allow_repeat=False,
               include_procedure=False):
        self.calls.append((query, phase))
        return FakeResult(self.mapping.get(query, []))


def write_tasks(tmp_path, tasks):
    path = tmp_path / "tasks.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for task in tasks:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
    return str(path)


def test_load_tasks_accepts_jsonl_and_json_array(tmp_path):
    jsonl = write_tasks(tmp_path, [{"id": "a", "query": "q1"}])
    assert bench.load_tasks(jsonl)[0]["id"] == "a"

    path = tmp_path / "tasks.json"
    path.write_text(json.dumps([{"query": "q2"}]), encoding="utf-8")
    tasks = bench.load_tasks(str(path))
    assert tasks[0]["id"] == "task-1" and tasks[0]["phase"] == "P2"


def test_load_tasks_rejects_rows_without_query(tmp_path):
    path = write_tasks(tmp_path, [{"id": "a"}])
    try:
        bench.load_tasks(path)
    except ValueError as e:
        assert "query" in str(e)
    else:  # pragma: no cover
        raise AssertionError("缺 query 应当报错，而不是静默跳过")


def test_evaluate_counts_hits_and_context_cost(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    retriever = FakeRetriever({"命中查询": ["[沉淀|env-windows|2026-09-13|0.85] 用 pwsh 7 或带 BOM"],
                               "空查询": []})
    report = bench.evaluate(cfg, [
        {"id": "hit", "query": "命中查询", "phase": "P2", "expect": ["BOM"]},
        {"id": "miss", "query": "命中查询", "phase": "P2", "expect": ["不存在的词"]},
        {"id": "empty", "query": "空查询", "phase": "P3", "expect": ["任意"]},
        {"id": "observe", "query": "命中查询", "phase": "P0", "expect": []},
    ], retriever=retriever)

    assert report["tasks"] == 4 and report["judged"] == 3 and report["observed_only"] == 1
    assert report["on"]["hit"] == 1
    assert report["on"]["hit_rate"] == round(1 / 3, 3)     # 只观察的那行不进分母
    assert report["misses"] == ["miss", "empty"]
    assert report["empty"] == ["empty"]
    assert report["off"]["hit_rate"] == 0.0 and report["off"]["chars_total"] == 0
    assert report["on"]["chars_total"] > 0


def test_render_states_the_scope_honestly(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    report = bench.evaluate(cfg, [{"id": "x", "query": "q", "phase": "P2", "expect": ["nope"]}],
                            retriever=FakeRetriever({"q": ["一些召回内容"]}))
    text = bench.render(report)
    assert "入口覆盖" in text and "agent harness" in text   # 不许把检索基准说成任务成功率
    assert "未命中：x" in text


def test_default_task_path_lives_in_state(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    assert bench.default_task_path(cfg).endswith("bench-tasks.jsonl")
    assert str(cfg.state_dir) in bench.default_task_path(cfg)   # 在 state/ 下 → 不进仓库
