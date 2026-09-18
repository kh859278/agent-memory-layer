"""只读本地视图：把已有的 JSON 状态汇总成**一个静态 HTML**（一页看完）。

## 为什么不做交互式 Web UI

本机这套东西是 **09:30 的计划任务**在跑，不是给人天天点的工作台：

  · 交互式 UI 意味着要常驻一个服务/端口。计划任务跑完就退，没人在那里守着端口；
    真要常驻就多一个"半夜挂掉的进程"，而没人会去看它有没有挂。
  · 一旦有服务就有**写操作**的可能（按钮、表单、SSE）。这个视图的定位是"看完就行"，
    根本不需要写；**能力的上限就是没有写入口**——比"写入口加了鉴权"更省心。
  · 静态文件可以直接双击打开、可以丢进任何浏览器、可以随报告一起归档；
    无需 JS 还意味着它**不会因为浏览器策略变更而报废**，也不会发任何网络请求。

所以：单文件 HTML、内联 `<style>`、**零 JS、零外链**，内容全部来自磁盘上已有的 JSON。

## 刻意不放进页面的东西

  · **技能正文**（`SKILL.md` 的 body）：那是程序性内容，页面会被分享/归档；
    只放技能名、路径、哈希、状态这些"身份信息"。
  · **任何 http/https 链接**：连"去 PyPI 看新版"这种都不放——一个自包含的页面
    不该在离线/内网环境里变成一堆死链，也不该在打开时向外发请求。
  · 注入过的记忆正文（bench 报告里本来就不落盘，这里只取汇总字段）。

数据源全部**容错**：取不到就是空值，页面照常生成。计划任务里最怕是"因为视图崩了，
把整轮巡检也带崩"——所以 `build()` 的每个字段都是独立的 try。
"""
from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

from ..text import write_lf
from . import capability, scopes, sources

TITLE = "技能与记忆层状态（只读）"
# 报告列表只列最近几个：页面是"一屏看完"用的，不是归档浏览器
REPORTS_LIMIT = 5
# 来源表只放这些字段（其余字段是内部账目，页面不需要）
SOURCE_FIELDS = ("repo", "scope", "layout", "subdir", "branch", "enabled", "priority", "added_at")
# bench 报告只取汇总字段：完整报告里有逐行判分，页面放不下也没必要
BENCH_FIELDS = ("tasks", "runs", "arms", "delta")


# ------------------------------------------------------------------ 取数

def output_path(cfg) -> Path:
    return cfg.state_dir / "patrol" / "ui" / "index.html"


def _recent_files(directory, suffixes=(".json",), limit: int = 5) -> list:
    """目录里最近修改的若干文件：`[{name, mtime, bytes}]`（**只 stat，不读内容**）。"""
    try:
        root = Path(directory)
        if not root.is_dir():
            return []
        entries = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in suffixes]
    except OSError:
        return []
    rows = []
    for path in entries:
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append({"name": path.name, "mtime": _fmt_time(stat.st_mtime), "bytes": stat.st_size})
    rows.sort(key=lambda r: str(r["mtime"]), reverse=True)
    return rows[:limit]


def _fmt_time(timestamp) -> str:
    try:
        return dt.datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, TypeError, ValueError):
        return ""


def _newest_json(directory) -> Path | None:
    """目录里最新的一个 JSON 文件（按 mtime）。"""
    try:
        root = Path(directory)
        if not root.is_dir():
            return None
        files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() == ".json"]
    except OSError:
        return None
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _bench_summary(cfg) -> dict | None:
    """task-runs 里最近一份报告的**汇总字段**（`tasks/runs/arms/delta`）。

    为什么不放全量：报告里每一行都有判分与任务 id，页面是给人"现在怎么样"的概览，
    细节该去 `--json` 或报告文件里看。取不到就 None（页面显示"还没有 bench 报告"）。
    """
    path = _newest_json(cfg.state_dir / "bench" / "task-runs")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"file": path.name, "error": "报告读不出来（JSON 坏了？）"}
    if not isinstance(data, dict):
        return {"file": path.name, "error": "报告不是对象"}
    try:
        mtime = _fmt_time(path.stat().st_mtime)
    except OSError:
        # 报告文件在读到一半时被删了（巡检会滚动覆盖报告）：降级成空时间，不能炸
        mtime = ""
    summary = {"file": path.name, "mtime": mtime}
    for key in BENCH_FIELDS:
        summary[key] = data.get(key)
    return summary


def build(cfg) -> dict:
    """汇总页面要用的全部数据（**任何一块取不到都不抛**）。"""
    data: dict = {"generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  "home": str(getattr(cfg, "home", "") or ""),
                  "errors": []}

    try:
        data["scopes"] = scopes.summary(cfg)
    except Exception as e:  # noqa: BLE001 - 视图崩了不该带崩巡检
        data["scopes"] = []
        data["errors"].append(f"作用域读不出来：{type(e).__name__}")

    try:
        data["sources"] = [{k: source.get(k) for k in SOURCE_FIELDS} for source in sources.load(cfg)]
    except Exception as e:  # noqa: BLE001
        data["sources"] = []
        data["errors"].append(f"来源读不出来：{type(e).__name__}")

    try:
        data["lifecycle"] = (capability.overview(cfg).get("skills") or [])
    except Exception as e:  # noqa: BLE001
        data["lifecycle"] = []
        data["errors"].append(f"生命周期读不出来：{type(e).__name__}")

    try:
        from . import skills as skills_mod
        data["pending"] = skills_mod.pending_items(cfg)
    except Exception as e:  # noqa: BLE001
        data["pending"] = []
        data["errors"].append(f"待批列表读不出来：{type(e).__name__}")

    try:
        from .notify import NoticeQueue
        data["notify"] = NoticeQueue(cfg).render()
    except Exception as e:  # noqa: BLE001 - 通知队列坏了只影响这一块
        data["notify"] = ""
        data["errors"].append(f"通知队列读不出来：{type(e).__name__}")

    data["reports"] = _recent_files(cfg.state_dir / "patrol" / "reports", limit=REPORTS_LIMIT)
    data["bench"] = _bench_summary(cfg)

    rows = data["lifecycle"]
    data["lifecycle_counts"] = {}
    for row in rows:
        state = str(row.get("state") or "discovered")
        data["lifecycle_counts"][state] = data["lifecycle_counts"].get(state, 0) + 1
    return data


# ------------------------------------------------------------------ 渲染

def esc(value) -> str:
    """所有动态值都必须过这里。

    为什么不用"参数都是自己拼的所以安全"这套说辞：技能名/路径/仓库名**来自上游仓库**，
    完全可能带 `<script>`（`patrol diff` 的威胁模型里就有"上游投毒"这一条）。
    这个文件用浏览器打开，一个未转义的技能名就等于在上游仓库里放了个 XSS。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    return html.escape(str(value), quote=True)


def _cell(value) -> str:
    if value is None or value == "":
        return '<span class="dim">—</span>'
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return esc(value)


def _table(headers: list, rows: list) -> str:
    """一个表格；`rows` 里每行是已渲染好的 `<td>` 字符串（避免二次转义）。"""
    if not rows:
        return '<p class="dim">（空）</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(f"<tr>{row}</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _section(title: str, inner: str, note: str = "") -> str:
    hint = f'<p class="dim">{esc(note)}</p>' if note else ""
    return f"<section><h2>{esc(title)}</h2>{hint}{inner}</section>"


def _scopes_section(data: dict) -> str:
    rows = []
    for row in data.get("scopes") or []:
        state = "已同步知识库" if row.get("sync_kb") else "只在本地"
        if not row.get("exists"):
            state += "（目录不存在）"
        rows.append("".join([
            f"<td>{_cell(row.get('scope'))}</td>",
            f"<td>{_cell(row.get('kind'))}</td>",
            f"<td>{_cell(row.get('name'))}</td>",
            f"<td>{_cell(row.get('skills'))}</td>",
            f"<td>{_cell(state)}</td>",
            f"<td class=\"path\">{_cell(row.get('path'))}</td>",
        ]))
    return _table(["作用域", "类型", "技能目录", "技能数", "知识库", "路径"], rows)


def _sources_section(data: dict) -> str:
    rows = []
    for row in data.get("sources") or []:
        rows.append("".join([
            f"<td>{_cell(row.get('repo'))}</td>",
            f"<td>{_cell(row.get('scope'))}</td>",
            f"<td>{_cell(row.get('layout'))}</td>",
            f"<td>{_cell(row.get('subdir'))}</td>",
            f"<td>{_cell(row.get('enabled'))}</td>",
            f"<td>{_cell(row.get('priority'))}</td>",
        ]))
    return _table(["仓库", "作用域", "布局", "子目录", "启用", "优先级"], rows)


def _lifecycle_section(data: dict) -> str:
    counts = data.get("lifecycle_counts") or {}
    summary = "、".join(f"{esc(k)} {esc(v)}" for k, v in sorted(counts.items())) or "（还没登记）"
    rows = []
    for row in sorted(data.get("lifecycle") or [],
                      key=lambda r: (str(r.get("state") or ""), str(r.get("name") or ""))):
        flags = []
        flags.append("有声明" if row.get("has_declaration") else "无声明")
        if row.get("requires_approval"):
            flags.append("需批准")
        if row.get("undeclared_high_risk"):
            flags.append("⚠未声明高危：" + ",".join(str(x) for x in row["undeclared_high_risk"]))
        elif row.get("undeclared"):
            flags.append("未声明：" + ",".join(str(x) for x in row["undeclared"]))
        rows.append("".join([
            f"<td>{_cell(row.get('name'))}</td>",
            f"<td>{_cell(row.get('state'))}</td>",
            f"<td>{_cell(', '.join(str(x) for x in (row.get('declared') or [])))}</td>",
            f"<td>{_cell('；'.join(flags))}</td>",
        ]))
    inner = f'<p class="dim">状态分布：{summary}</p>'
    inner += _table(["技能", "生命周期", "声明能力", "标记"], rows)
    return _section("技能生命周期", inner)


def _pending_section(data: dict) -> str:
    rows = []
    for row in data.get("pending") or []:
        rows.append("".join([
            f"<td>{_cell(row.get('name'))}</td>",
            f"<td class=\"path\">{_cell(row.get('path'))}</td>",
        ]))
    return _section("待批（暂存在 _pending，等人 accept）",
                    _table(["技能", "暂存路径"], rows),
                    "采纳：aml patrol accept <技能名>；这里只是只读列表。")


def _reports_section(data: dict) -> str:
    rows = []
    for row in data.get("reports") or []:
        rows.append("".join([
            f"<td class=\"path\">{_cell(row.get('name'))}</td>",
            f"<td>{_cell(row.get('mtime'))}</td>",
            f"<td>{_cell(row.get('bytes'))}</td>",
        ]))
    return _section(f"最近 {REPORTS_LIMIT} 份巡检报告（只列文件名，不读内容）",
                    _table(["文件", "修改时间", "字节"], rows))


def _bench_section(data: dict) -> str:
    bench = data.get("bench")
    if not bench:
        return _section("最近一次 bench", '<p class="dim">（还没有 bench 报告）</p>')
    if bench.get("error"):
        return _section("最近一次 bench",
                        f'<p class="warn">{esc(bench.get("file"))}：{esc(bench.get("error"))}</p>')
    rows = ["".join([f"<td>{esc(k)}</td>", f"<td>{_cell(bench.get(k))}</td>"])
            for k in BENCH_FIELDS]
    inner = f'<p class="dim">文件：{esc(bench.get("file"))}（{esc(bench.get("mtime"))}）</p>'
    inner += _table(["字段", "值"], rows)
    return _section("最近一次 bench（只取汇总字段）", inner)


def _notify_section(data: dict) -> str:
    text = data.get("notify") or ""
    if not text:
        return _section("通知队列", '<p class="dim">（读不出来 / 队列为空）</p>')
    return _section("通知队列（纯文本）", f"<pre>{esc(text)}</pre>")


def _errors(data: dict) -> str:
    errors = data.get("errors") or []
    if not errors:
        return ""
    items = "".join(f"<li>{esc(e)}</li>" for e in errors)
    return f'<div class="warn"><p>以下数据块没取到（页面其余部分照常）：</p><ul>{items}</ul></div>'


STYLE = """
:root { color-scheme: light dark; }
body { font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0 auto;
       max-width: 1100px; padding: 24px 20px 60px; line-height: 1.55; }
h1 { font-size: 1.35rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 0 0 6px; border-bottom: 1px solid #8884; padding-bottom: 4px; }
section { margin: 22px 0; }
table { border-collapse: collapse; width: 100%; font-size: 0.88rem; }
th, td { border: 1px solid #8884; padding: 3px 7px; text-align: left; vertical-align: top; }
th { background: #8883; font-weight: 600; }
td.path { word-break: break-all; font-family: ui-monospace, Consolas, monospace; font-size: 0.82rem; }
.dim { color: #7778; font-size: 0.85rem; }
.warn { border: 1px solid #e0a030; background: #e0a03018; padding: 6px 10px; border-radius: 4px; }
pre { white-space: pre-wrap; font-size: 0.84rem; background: #8881; padding: 8px; border-radius: 4px; }
footer { margin-top: 30px; color: #7778; font-size: 0.8rem; }
"""


def render_html(data: dict) -> str:
    """单文件 HTML：内联样式、无 JS、无外链、动态值全部转义。

    加 `<meta charset>` 是必须的（不是可选）：没有它浏览器会按本地默认编码猜，
    中文 Windows 上就是 GBK → 整页乱码，而"打开是乱码"会让人以为工具坏了。
    """
    sections = "\n".join([
        _section("作用域与技能目录", _scopes_section(data)),
        _section("来源（按优先级，来源文件是唯一真相）", _sources_section(data)),
        _lifecycle_section(data),
        _pending_section(data),
        _reports_section(data),
        _bench_section(data),
        _notify_section(data),
    ])
    counts = f'{len(data.get("lifecycle") or [])} 个技能'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(TITLE)}</title>
<style>{STYLE}</style>
</head>
<body>
<h1>{esc(TITLE)}</h1>
<p class="dim">生成时间 {esc(data.get("generated_at"))} · 数据目录 {esc(data.get("home"))}
· 共 {esc(counts)} · 本页只读、无脚本、无外部请求</p>
{_errors(data)}
{sections}
<footer>由 aml 生成（aml patrol ui）。内容来自 state/ 下的 JSON 汇总；技能正文与记忆正文不在此页。</footer>
</body>
</html>
"""


def write(cfg, out=None) -> str:
    """写出静态页面，返回路径字符串（UTF-8、LF 换行）。

    强制 LF（`write_lf`）的理由不只是洁癖：这文件常被拷进知识库/仓库，
    混合换行会让 diff 整段变红；另外 `Path.write_text(newline=...)` 是 3.10+ 才有的参数。
    """
    target = Path(out) if out else output_path(cfg)
    write_lf(target, render_html(build(cfg)))
    return str(target)


__all__ = ["TITLE", "REPORTS_LIMIT", "SOURCE_FIELDS", "BENCH_FIELDS", "output_path", "build",
           "esc", "render_html", "write"]
