"""可复现基准：memory ON vs memory OFF（**检索层**口径，不是任务成功率）。

⚠️ 先说实话，免得被误读：这个基准量的是
    · 该任务"应该被想起来的经验"有没有被检索到（hit rate）
    · 为了拿到它，往上下文里塞了多少字（context cost）
  **不是**"任务成功率提升多少"。后者需要一个 agent harness（真跑任务、判成败），
  属于下一步工作（见 ROADMAP Step 4）。

为什么还值得先做这一层：检索是整个系统的入口 —— 入口漏了，后面再准也没用；
而这层**完全可复现**（同一份任务表 + 同一个库 → 同一个数字），不需要跑 agent、不花 token。

任务表格式（JSONL，一行一个任务）：
    {"id": "win-encoding", "phase": "P2", "query": "powershell 编码 乱码",
     "expect": ["GBK", "UTF-8"], "note": "应命中 Windows 编码那条沉淀"}

`expect` 是"必须出现在召回文本里的子串"（任一到即算命中）。
任务表本身**不进仓库**（里面往往带你的项目细节）——仓库只放 `tasks.example.jsonl` 模板。
"""
from __future__ import annotations

import json

from .retrieval import Retriever


def load_tasks(path: str) -> list:
    """读任务表（JSONL 或 JSON 数组）。缺 query 的行直接报错，别静默跳过。"""
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return []
    if text.startswith("["):
        tasks = json.loads(text)
    else:
        tasks = [json.loads(line) for line in text.splitlines() if line.strip()]
    for index, task in enumerate(tasks, 1):
        if not task.get("query"):
            raise ValueError(f"第 {index} 行缺少 query 字段")
        task.setdefault("id", f"task-{index}")
        task.setdefault("phase", "P2")
        task.setdefault("expect", [])
    return tasks


def hit(text: str, expect: list) -> bool:
    """命中判据：expect 里**任一个**子串出现在召回文本里。

    `expect` 为空的行不判命中（`judged=False`）—— 那种行是用来"看看能不能召回"的，
    把它算成未命中会污染命中率（模板里第三条就是这个用途）。
    """
    if not expect:
        return False
    return any(item in text for item in expect)


def evaluate(cfg, tasks: list, retriever: Retriever | None = None, log=None) -> dict:
    """跑一遍：memory ON（真检索）与 memory OFF（不检索）对照。"""
    retriever = retriever or Retriever(cfg)
    rows = []
    for task in tasks:
        result = retriever.search(task["query"], phase=task["phase"],
                                  project=task.get("project"), tag=task.get("tag"),
                                  allow_repeat=True)
        text = "\n".join(result.lines)
        row = {"id": task["id"], "phase": task["phase"], "query": task["query"],
               "hits": len(result.lines), "chars": result.used,
               "judged": bool(task.get("expect")),
               "on_hit": hit(text, task.get("expect") or []),
               "empty": result.empty, "diag": result.diag}
        rows.append(row)
        if log:
            mark = "命中" if row["on_hit"] else ("未命中" if not result.empty else "空")
            log(f"  [{mark}] {row['id']:<20} {row['phase']}  条数 {row['hits']}  "
                f"字数 {row['chars']}  {task['query'][:34]}")
    return summarize(rows)


def summarize(rows: list) -> dict:
    judged = [r for r in rows if r.get("judged")]
    observed = [r for r in rows if not r.get("judged")]
    on_hits = sum(1 for r in judged if r["on_hit"])
    chars = sum(r["chars"] for r in rows)
    return {
        "tasks": len(rows),
        "judged": len(judged),
        "observed_only": len(observed),
        # memory ON：真的去检索
        "on": {"hit": on_hits,
               "hit_rate": round(on_hits / len(judged), 3) if judged else 0.0,
               "chars_total": chars,
               "chars_avg": round(chars / len(rows), 1) if rows else 0.0},
        # memory OFF：不检索 → 命中 0、上下文 0 字（这就是对照基线）
        "off": {"hit": 0, "hit_rate": 0.0, "chars_total": 0, "chars_avg": 0.0},
        "misses": [r["id"] for r in judged if not r["on_hit"]],
        "empty": [r["id"] for r in rows if r["empty"]],
        "rows": rows,
    }


def render(report: dict) -> str:
    on, off = report["on"], report["off"]
    scope = f"{report.get('judged', report['tasks'])} 个判定任务" + (
        f" + {report['observed_only']} 个只观察" if report.get("observed_only") else "")
    lines = [f"检索基准（共 {report['tasks']} 个任务：{scope}）",
             f"  memory ON ：命中 {on['hit']}/{report.get('judged', report['tasks'])}"
             f"（{on['hit_rate']:.0%}）｜平均注入 {on['chars_avg']} 字",
             f"  memory OFF：命中 {off['hit']}/{report.get('judged', report['tasks'])}"
             f"（基线：不检索、0 字）",
             "  → 这一层量的是**入口覆盖**与上下文成本；任务成功率需要 agent harness"
             "（见 ROADMAP Step 4）"]
    if report.get("misses"):
        lines.append(f"  未命中：{', '.join(report['misses'])}")
    if report.get("empty"):
        lines.append(f"  完全查不到：{', '.join(report['empty'])}（先看诊断：换措辞/放宽阈值/补写回）")
    return "\n".join(lines)


def default_task_path(cfg) -> str:
    return str(cfg.state_dir / "bench-tasks.jsonl")
