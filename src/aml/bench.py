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


def _parse_jsonl(text: str, path: str) -> list:
    """逐行解析；**整块多行 JSON 也吃**（人工写的任务表常被格式化过）。

    踩过的坑：示例任务表为了可读性把一条任务写成两行，而这里只按行 parse，
    于是"复制示例 → 直接跑"必然 JSONDecodeError。现在先逐行试，失败就退一步
    用 `raw_decode` 把整个文件当成"一串 JSON 值"流式解析（对象可以跨行）。
    """
    tasks, broken = [], []
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            tasks.append(json.loads(line))
        except ValueError as e:
            broken.append((index, line.strip()[:60], str(e)))
    if not broken:
        return tasks
    decoder = json.JSONDecoder()
    stream, pos, end = [], 0, len(text)
    try:
        while pos < end:
            while pos < end and text[pos] in " \t\r\n":
                pos += 1
            if pos >= end:
                break
            obj, pos = decoder.raw_decode(text, pos)
            stream.append(obj)
    except ValueError:
        line_no, snippet, detail = broken[0]
        raise ValueError(f"{path}:{line_no} 解析失败（{detail}）：{snippet}") from None
    return stream


def load_tasks(path: str) -> list:
    """读任务表（JSONL 或 JSON 数组）。缺 query 的行直接报错，别静默跳过。"""
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return []
    if text.startswith("["):
        tasks = json.loads(text)
    else:
        tasks = _parse_jsonl(text, path)
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


def _percentiles(values: list, points=(50, 95, 99)) -> dict:
    """注入量的分位数：**平均值会掩盖尾部** —— 一次 P1 注入 1500 字才是真实成本。"""
    clean = sorted(v for v in values if isinstance(v, (int, float)))
    if not clean:
        return {}
    out = {}
    for point in points:
        index = min(len(clean) - 1, max(0, int(round(point / 100 * (len(clean) - 1)))))
        out[f"p{point}"] = clean[index]
    out["max"] = clean[-1]
    return out


def score_lines(lines: list, relevant: list, top: int = 3) -> dict:
    """前 top 条里有多少**真的相关**（Precision@k），以及有没有命中（Recall@k）。

    为什么要它：`hit` 只看"召回文本里出现过期望串" —— 一份把 3 条无关记忆和 1 条对的
    一起塞进去的召回也算"命中"。Precision@3 直接量"塞进来的东西有多少是废的"，
    这是"注入错误记忆比漏召回更危险"的可测版本。
    """
    if not relevant:
        return {"precision_at3": None, "recall_at3": None}
    head = lines[:top]
    if not head:
        return {"precision_at3": 0.0, "recall_at3": False}
    good = sum(1 for line in head if hit(line, relevant))
    return {"precision_at3": round(good / len(head), 3), "recall_at3": good > 0}


def evaluate(cfg, tasks: list, retriever: Retriever | None = None, log=None,
             form: str = "query") -> dict:
    """跑一遍：memory ON（真检索）与 memory OFF（不检索）对照。

    `form` 决定用哪种问法：`query`（原问法，往往就是知识标题 —— **偏易，有查询泄漏风险**）
    或 `alt`（任务里的 `alt_query`：人真正会问的那种句子）。
    两种问法的差值才说明"入口在真实提问下还靠不靠得住"。
    """
    retriever = retriever or Retriever(cfg)
    rows = []
    for task in tasks:
        query = task.get("alt_query") if form == "alt" else task["query"]
        if not query:
            continue
        result = retriever.search(query, phase=task["phase"],
                                  project=task.get("project"), tag=task.get("tag"),
                                  allow_repeat=True)
        text = "\n".join(result.lines)
        relevant = task.get("relevant") or task.get("expect") or []
        row = {"id": task["id"], "phase": task["phase"], "query": query, "form": form,
               "hits": len(result.lines), "chars": result.used,
               "judged": bool(task.get("expect")),
               "on_hit": hit(text, task.get("expect") or []),
               "empty": result.empty, "diag": result.diag}
        row.update(score_lines(result.lines, relevant))
        rows.append(row)
        if log:
            mark = "命中" if row["on_hit"] else ("未命中" if not result.empty else "空")
            p3 = row.get("precision_at3")
            log(f"  [{mark}] {row['id']:<20} {row['phase']}  条数 {row['hits']}  "
                f"字数 {row['chars']}  P@3 {p3 if p3 is not None else '-'}  {query[:30]}")
    return summarize(rows)


def summarize(rows: list) -> dict:
    judged = [r for r in rows if r.get("judged")]
    observed = [r for r in rows if not r.get("judged")]
    on_hits = sum(1 for r in judged if r["on_hit"])
    chars = sum(r["chars"] for r in rows)
    scored = [r for r in rows if r.get("precision_at3") is not None]
    return {
        "tasks": len(rows),
        "judged": len(judged),
        "observed_only": len(observed),
        "form": rows[0].get("form") if rows else None,
        # memory ON：真的去检索
        "on": {"hit": on_hits,
               "hit_rate": round(on_hits / len(judged), 3) if judged else 0.0,
               "chars_total": chars,
               "chars_avg": round(chars / len(rows), 1) if rows else 0.0,
               "chars_pct": _percentiles([r["chars"] for r in rows]),
               "precision_at3": (round(sum(r["precision_at3"] for r in scored) / len(scored), 3)
                                 if scored else None),
               "recall_at3": (round(sum(1 for r in scored if r["recall_at3"]) / len(scored), 3)
                              if scored else None)},
        # memory OFF：不检索 → 命中 0、上下文 0 字（这就是对照基线）
        "off": {"hit": 0, "hit_rate": 0.0, "chars_total": 0, "chars_avg": 0.0},
        "misses": [r["id"] for r in judged if not r["on_hit"]],
        "empty": [r["id"] for r in rows if r["empty"]],
        "rows": rows,
    }


def evaluate_forms(cfg, tasks: list, forms=("query", "alt"), retriever: Retriever | None = None,
                   log=None) -> dict:
    """同一批任务换**问法**各跑一遍 —— 这一步就是为了暴露"查询泄漏"。"""
    retriever = retriever or Retriever(cfg)
    reports = {}
    for form in forms:
        usable = [t for t in tasks if (t.get("alt_query") if form == "alt" else t.get("query"))]
        if not usable:
            continue
        if log:
            log(f"— 问法 {form}（{len(usable)} 个任务）")
        reports[form] = evaluate(cfg, usable, retriever=retriever, log=log, form=form)
    return {"tasks": len(tasks), "forms": reports}


def render_forms(report: dict) -> str:
    forms = report.get("forms") or {}
    if not forms:
        return "没有可跑的问法（任务表里要有 `query`；另一种问法用 `alt_query`）"
    lines = ["问法对照（同库同任务，只换问法）"]
    for form, rep in forms.items():
        on = rep["on"]
        pct = on.get("chars_pct") or {}
        label = {"query": "原问法（偏易，常是知识标题）", "alt": "改写问法（人会这么问）"}.get(form, form)
        p3 = on.get("precision_at3")
        lines.append(f"  {form:<6} {label}")
        lines.append(f"         命中 {on['hit']}/{rep['judged']}（{on['hit_rate']:.0%}）｜"
                     f"平均注入 {on['chars_avg']} 字"
                     + (f"｜P50 {pct.get('p50')} / P95 {pct.get('p95')} / max {pct.get('max')}"
                        if pct else "")
                     + (f"｜P@3 {p3:.0%}｜R@3 {on['recall_at3']:.0%}" if p3 is not None else ""))
    if len(forms) > 1 and "query" in forms and "alt" in forms:
        gap = forms["query"]["on"]["hit_rate"] - forms["alt"]["on"]["hit_rate"]
        lines.append(f"  → 换算问法后命中率变化 {gap:+.0%}"
                     + ("（差距大 = 原问法在自我泄漏，别拿它当召回质量）" if gap > 0.15 else ""))
    return "\n".join(lines)


def render(report: dict) -> str:
    on, off = report["on"], report["off"]
    scope = f"{report.get('judged', report['tasks'])} 个判定任务" + (
        f" + {report['observed_only']} 个只观察" if report.get("observed_only") else "")
    pct = on.get("chars_pct") or {}
    p3, r3 = on.get("precision_at3"), on.get("recall_at3")
    lines = [f"检索基准（{report['tasks']} 个任务：{scope}；问法 {report.get('form') or 'query'}）",
             f"  memory ON ：命中 {on['hit']}/{report.get('judged', report['tasks'])}"
             f"（{on['hit_rate']:.0%}）｜平均注入 {on['chars_avg']} 字"
             + (f"｜P50 {pct.get('p50')} / P95 {pct.get('p95')} / max {pct.get('max')}" if pct else ""),
             f"  memory OFF：命中 {off['hit']}/{report.get('judged', report['tasks'])}"
             f"（基线：不检索、0 字）"]
    if p3 is not None:
        lines.append(f"  注入质量：P@3 {p3:.0%}（前 3 条里真正相关的比例）｜"
                     f"R@3 {r3:.0%}（前 3 条里至少有一条相关）")
    lines += ["  → 这一层量的是**入口覆盖**与上下文成本；任务成功率需要 agent harness"
              "（见 ROADMAP Step 4 的任务级基准）",
              "  → 平均值会掩盖尾部：P95 才是「某个任务被塞了多少字」的真实答案"]
    if report.get("misses"):
        lines.append(f"  未命中：{', '.join(report['misses'])}")
    if report.get("empty"):
        lines.append(f"  完全查不到：{', '.join(report['empty'])}（先看诊断：换措辞/放宽阈值/补写回）")
    return "\n".join(lines)


def default_task_path(cfg) -> str:
    return str(cfg.state_dir / "bench-tasks.jsonl")
