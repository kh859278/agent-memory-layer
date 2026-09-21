"""任务级基准（Step 4）：**真跑一个 agent**，memory ON vs memory OFF。

和 `bench.py`（检索基准）的分工，必须说清，否则数字会被误读：

| | 量什么 | 要不要花 token | 可复现性 |
|---|---|---|---|
| `bench.py`（检索层） | 该被想起来的经验**有没有被召回**、注入多少字 | 不要 | 完全可复现 |
| 本模块（任务层） | **任务做没做成**、返工几轮、多久、多少 token、有没有被记忆带偏、既有功能有没有被弄坏 | 要（真跑 agent） | 同模型同任务基本稳定，但不是逐字可复现 |

六个指标（都能从一次运行里客观算出来，不靠"感觉")：

1. `success`       —— 任务自带的验收命令退出码 0，且期望串出现、违禁串没出现
2. `rework`        —— agent 自己报告的轮数（`num_turns`）+ 改动文件数：返工代理量
3. `time`          —— 墙钟耗时与 agent 自报耗时
4. `tokens`/`cost` —— 输入/输出 token 与美元成本（**跑基准是要花钱的，报告里必须写出来**）
5. `forbidden`     —— 违禁串命中（任务里写清"哪些是过期/错误的做法"）→ 近似的"被记忆带偏率"
6. `regression`    —— 起跑前能过的既有检查，跑完后挂了 → "踩坏既有功能"率

诚实边界（写在报告里，也写在这里）：
  · memory OFF 组必须**显式禁用历史检索**，否则对照不成立（prompt 里会加这一句）
  · 任务表**不进仓库**（里面往往带项目细节），仓库只放模板与通用 fixture
  · agent 是"通用大脑 + 本机配置"，两组都可能作弊式地猜对 —— 所以只看**两组之差**，不看绝对值
  · 一次 run 只有 n 个任务，别把它当统计显著性证明；它是"有没有把事弄坏"的守门指标

任务表格式（JSONL，一行一个任务）：
    {"id": "py39-newline", "query": "python 3.9 write_text newline 不支持", "phase": "P2",
     "prompt": "……给 agent 的任务描述……",
     "fixture": "generic-python",          # tools/bench/fixtures/ 下的目录名，可省
     "verify": "python -m pytest -q",      # 验收命令，退出码 0 即通过
     "expect": ["1 passed"],               # 必须出现的子串（可省）
     "forbidden": ["write_text(newline"],  # 不许出现的子串（过期/错误做法）
     "expect_memory": ["write_lf"],        # 出现了就说明**真用了注入的经验**（可省）
     "regression": "python -m pytest -q",  # 起跑前先跑一遍，跑完再跑一遍（可省）
     "timeout": 600}

agent 契约（`--agent` 参数 / `AML_TASK_BENCH_AGENT` 环境变量）：
    命令从 **stdin 读任务 prompt**，往 stdout 打**一行 JSON**（claude -p 的格式即可）：
    {"result": "...", "num_turns": 3, "duration_ms": 12000, "total_cost_usd": 0.1,
     "usage": {"input_tokens": 1, "output_tokens": 1}, "is_error": false}
    少了哪个字段就按"未知"处理，不会因为这个判失败。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules",
             ".mypy_cache", ".idea", ".vscode"}
HIDDEN_DIR = "_hidden"        # fixture 里"判分时才给"的验收标准（agent 看不到）
FILE_SCAN_LIMIT = 200_000      # 违禁串扫描时读进内存的正文上限（防止把整个仓库读进来）
DEFAULT_TIMEOUT = 600

NO_MEMORY_RULE = (
    "【本次限制】不要使用任何历史记忆、知识库、检索工具或跨会话经验 —— "
    "只依据当前工作区里的文件完成任务。"
)
MEMORY_HEADER = (
    "【来自记忆层的检索结果】以下是历史沉淀（可能相关、也可能不相关，自己判断；"
    "过期的不要用）：\n"
)


# --------------------------------------------------------------------------- 任务表

def _as_cmd(value):
    """验收/回归命令：接受字符串或数组（数组更安全，带空格的路径不会被打散）。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    import shlex
    return shlex.split(str(value), posix=(os.name != "nt")) or str(value).split()


def load_tasks(path: str) -> list:
    """读任务表（JSONL 或 JSON 数组）。缺 prompt 的行直接报错，别静默跳过。"""
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        return []
    if text.startswith("["):
        tasks = json.loads(text)
    else:
        tasks = [json.loads(line) for line in text.splitlines() if line.strip()]
    for index, task in enumerate(tasks, 1):
        if not task.get("prompt"):
            raise ValueError(f"第 {index} 行缺少 prompt 字段")
        task.setdefault("id", f"task-{index}")
        task.setdefault("phase", "P2")
        task.setdefault("verify", ["python", "-m", "pytest", "-q"])
        task.setdefault("expect", [])
        task.setdefault("forbidden", [])
        task.setdefault("expect_memory", [])
        task.setdefault("timeout", DEFAULT_TIMEOUT)
        # 命令统一归一成数组：字符串里带空格的路径不会被 shlex 拆坏
        task["verify"] = _as_cmd(task["verify"])
        if task.get("regression"):
            task["regression"] = _as_cmd(task["regression"])
    return tasks


def fixture_root() -> str:
    """仓库自带的通用 fixture 目录（`tools/bench/fixtures`）。"""
    return str(_repo_root() / "tools" / "bench" / "fixtures")


def _repo_root():
    from pathlib import Path
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- 工作区

def snapshot(root) -> dict:
    """工作区指纹：相对路径 → 内容 sha1（跳过缓存/依赖目录）。"""
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                with open(full, "rb") as f:
                    out[rel] = hashlib.sha1(f.read()).hexdigest()
            except OSError:
                out[rel] = "unreadable"
    return out


def changed_files(before: dict, after: dict) -> list:
    """两次指纹之差：新增/修改/删除的文件（相对路径，已排序）。"""
    diff = [p for p in after if before.get(p) != after[p]]
    diff += [p for p in before if p not in after]
    return sorted(set(diff))


def prepare_workspace(task: dict, dest: str, fixtures: str | None = None,
                      include_hidden: bool = False) -> str:
    """把 fixture 拷进一次性工作目录（每次运行独立，跑完就删，不影响仓库）。

    `_hidden/` 里的东西**默认不给 agent**（判分前才拷进去）——
    这是任务级基准里唯一能真正区分"记得"与"猜得到"的手段：
    验收标准不在工作区里，agent 只能靠记忆层里那条跨会话经验。
    """
    os.makedirs(dest, exist_ok=True)
    name = task.get("fixture")
    if not name:
        return dest
    src = name if os.path.isabs(name) else os.path.join(fixtures or fixture_root(), name)
    if not os.path.isdir(src):
        raise FileNotFoundError(f"找不到 fixture：{src}")
    for entry in os.listdir(src):
        if entry in SKIP_DIRS or (entry == HIDDEN_DIR and not include_hidden):
            continue
        s, d = os.path.join(src, entry), os.path.join(dest, entry)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=shutil.ignore_patterns(*SKIP_DIRS))
        else:
            shutil.copy2(s, d)
    return dest


def inject_hidden(task: dict, dest: str, fixtures: str | None = None) -> list:
    """判分前把 `_hidden/` 拷进工作区，返回拷进去的相对路径（用于说明"验收标准不在场内"）。"""
    name = task.get("fixture")
    if not name:
        return []
    src = name if os.path.isabs(name) else os.path.join(fixtures or fixture_root(), name)
    hidden = os.path.join(src, HIDDEN_DIR)
    if not os.path.isdir(hidden):
        return []
    copied = []
    for base, dirs, files in os.walk(hidden):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            full = os.path.join(base, fname)
            rel = os.path.relpath(full, hidden)
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(full, target)
            copied.append(rel.replace("\\", "/"))
    return sorted(copied)


def build_prompt(task: dict, memory_block: str = "") -> str:
    """拼最终 prompt。OFF 组显式禁用记忆检索 —— 否则"对照"根本不成立。"""
    parts = [task["prompt"].strip()]
    if memory_block:
        parts.append(MEMORY_HEADER + memory_block)
    else:
        parts.append(NO_MEMORY_RULE)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- 跑 agent

def resolve_agent(argv):
    """把命令名解析成能跑的东西：Windows 上 npm 装的是 `.CMD`，直接跑即可。"""
    if not argv:
        return []
    head = argv[0]
    found = shutil.which(head) if not os.path.isfile(head) else head
    if not found:
        raise FileNotFoundError(f"找不到 agent 命令：{head}（用 --agent 指定，或设 AML_TASK_BENCH_AGENT）")
    return [found] + [str(a) for a in argv[1:]]


def default_agent() -> list:
    """默认跑 claude（本机已装、`-p` 出 JSON）。换 agent 用 --agent / AML_TASK_BENCH_AGENT。"""
    env = os.environ.get("AML_TASK_BENCH_AGENT")
    if env:
        import shlex
        return shlex.split(env, posix=(os.name != "nt"))
    return ["claude", "-p", "--output-format", "json",
            "--no-session-persistence",              # 别把基准跑出来的会话灌进记忆层
            "--permission-mode", "bypassPermissions"]


def _parse_agent_stdout(stdout: str) -> dict:
    """从 stdout 里抠出结果 JSON：整段优先，否则从后往前找第一行能解析的。"""
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def run_agent(argv, prompt: str, cwd: str, timeout: int = DEFAULT_TIMEOUT,
              env: dict | None = None) -> dict:
    """跑一次 agent：prompt 走 stdin，stdout 收 JSON。超时/崩溃都不抛，记成错误字段。"""
    run_env = dict(os.environ)
    run_env["PYTHONIOENCODING"] = "utf-8"
    run_env.pop("AML_TASK_BENCH_CHILD", None)
    if env:
        run_env.update(env)
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", cwd=cwd,
                              timeout=timeout, env=run_env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"超时（>{timeout}s）", "wall_ms": int(timeout * 1000),
                "result_text": "", "turns": None, "duration_ms": None, "tokens_in": None,
                "tokens_out": None, "cache_read": None, "cost_usd": None, "denials": []}
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}",
                "wall_ms": int((time.monotonic() - started) * 1000), "result_text": "",
                "turns": None, "duration_ms": None, "tokens_in": None, "tokens_out": None,
                "cache_read": None, "cost_usd": None, "denials": []}

    wall_ms = int((time.monotonic() - started) * 1000)
    data = _parse_agent_stdout(proc.stdout)
    usage = data.get("usage") or {}
    out = {
        # 没解析出 JSON 也算失败：否则"agent 什么都没说"会被当成成功，指标全假
        "ok": proc.returncode == 0 and bool(data) and not data.get("is_error", False),
        "error": None if proc.returncode == 0 else f"agent 退出码 {proc.returncode}",
        "stderr": (proc.stderr or "").strip()[-400:],
        "wall_ms": wall_ms,
        "result_text": str(data.get("result") or ""),
        "turns": data.get("num_turns"),
        "duration_ms": data.get("duration_ms"),
        "tokens_in": usage.get("input_tokens"),
        "tokens_out": usage.get("output_tokens"),
        "cache_read": usage.get("cache_read_input_tokens"),
        "cost_usd": data.get("total_cost_usd"),
        "denials": data.get("permission_denials") or [],
    }
    if data.get("is_error"):
        out["error"] = str(data.get("subtype") or "agent 报告失败")
    if not data:
        out["error"] = out["error"] or "agent 没有输出可解析的 JSON"
    return out


# --------------------------------------------------------------------------- 判分

def run_check(cmd, cwd: str, timeout: int = 300) -> dict:
    """跑验收/回归命令，返回退出码与合并输出（不抛异常）。

    `PYTHONIOENCODING=utf-8` 是必须的：中文 Windows 上子进程的 stdout 会按 GBK 编码，
    而我们在父进程按 UTF-8 解码（见 `text.ensure_utf8_stdio` 的同一课）——
    不解码对齐的话，判分输出全是乱码，排查时看不出到底哪里挂了。
    """
    if not cmd:
        return {"cmd": None, "rc": None, "output": "", "skipped": True}
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=cwd, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "rc": 124, "output": f"验收命令超时（>{timeout}s）", "skipped": False}
    except OSError as e:
        return {"cmd": cmd, "rc": 127, "output": f"验收命令跑不起来：{e}", "skipped": False}
    return {"cmd": cmd, "rc": proc.returncode,
            "output": ((proc.stdout or "") + (proc.stderr or "")).strip()[-4000:],
            "skipped": False}


def _workspace_text(root, limit: int = FILE_SCAN_LIMIT) -> str:
    """工作区正文（截断）：违禁串/记忆使用痕迹要连"写进代码里"一起查。"""
    chunks, used = [], 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            full = os.path.join(base, name)
            try:
                with open(full, encoding="utf-8", errors="ignore") as f:
                    body = f.read()
            except OSError:
                continue
            chunks.append(body)
            used += len(body)
            if used >= limit:
                return "\n".join(chunks)
    return "\n".join(chunks)


def grade(task: dict, run: dict, workspace: str, before: dict, after: dict,
          baseline: dict | None = None, verify: dict | None = None,
          workspace_text: str | None = None) -> dict:
    """判分。`verify`/`baseline` 可在外面先跑好传进来（省一次重复执行）。

    `workspace_text` 必须是**注入 `_hidden/` 之前**的正文 —— 验收标准自己的正文里往往
    就写着正确做法（`encoding="utf-8"` 之类），混进来会把 `memory_used` 变成"永远命中"。
    """
    verify = verify if verify is not None else run_check(_as_cmd(task.get("verify")), workspace)
    regression = run_check(_as_cmd(task.get("regression")), workspace) if task.get("regression") else None
    agent_text = workspace_text if workspace_text is not None else _workspace_text(workspace)
    probe = "\n".join([run.get("result_text") or "", verify.get("output") or "", agent_text])
    expects = task.get("expect") or []
    missing = [e for e in expects if e not in probe]
    forbidden = [f for f in (task.get("forbidden") or []) if f in probe]
    memory_used = [m for m in (task.get("expect_memory") or []) if m in probe]
    changed = changed_files(before, after)
    succeeded = verify.get("rc") == 0 and not missing and not forbidden
    # 回归：起跑前能过、跑完挂了 —— 这才是"踩坏既有功能"；起跑前就挂的不算账
    regressed = bool(regression and baseline and baseline.get("rc") == 0
                     and regression.get("rc") != 0)
    return {
        "verify_rc": verify.get("rc"), "verify_output": verify.get("output"),
        "missing_expect": missing, "forbidden_hits": forbidden,
        "memory_used": memory_used, "changed_files": changed,
        "success": succeeded, "regressed": regressed,
        "baseline_rc": (baseline or {}).get("rc"),
    }


# --------------------------------------------------------------------------- 检索注入

def retrieve(cfg, task: dict, log=None) -> dict:
    """memory ON 组：真的去检索，并把每条命中**连 hash 带正文**记下来。

    为什么要带正文：自动反馈要能**精确归因** —— 只有"这条记忆自己的正文里就写着那个
    被用上的做法"，才敢给它记 `worked`；否则一律只记 `used`（被用过，不主张有用）。
    """
    from .retrieval import Retriever
    query = task.get("query") or task["prompt"]
    retriever = Retriever(cfg)
    result = retriever.search(query, phase=task.get("phase", "P2"), project=task.get("project"),
                              tag=task.get("tag"), allow_repeat=True)
    hashes = list(getattr(result, "hashes", []) or [])
    items = []
    for index, line in enumerate(result.lines):
        items.append({"hash": hashes[index] if index < len(hashes) else "",
                      "text": line, "chars": len(line)})
    return {"text": "\n".join(result.lines), "hashes": hashes, "items": items,
            "lines": len(result.lines), "chars": result.used, "empty": result.empty,
            "diag": result.diag}


# --------------------------------------------------------------------------- 主流程

def execute(cfg, task: dict, arm: str, agent: list, fixtures: str | None = None,
            keep: bool = False, workdir: str | None = None, env: dict | None = None,
            log=None, ablate: int = 0) -> dict:
    """跑一个任务的一个分组（arm ∈ off / on / ablate），返回一行结果。

    `ablate=N`：ON 组但**藏掉排在最前的 N 条记忆**再跑 —— 反事实臂，
    用来回答"注入的记忆到底有没有被用上"（两组成败一样 → 那些记忆可能只是装饰）。
    """
    workdir = workdir or tempfile.mkdtemp(prefix=f"aml-bench-{task['id']}-{arm}-")
    log = log or (lambda *_a, **_k: None)
    memory = {"text": "", "hashes": [], "chars": 0, "lines": 0, "empty": True, "diag": {}}
    try:
        prepare_workspace(task, workdir, fixtures)
        before = snapshot(workdir)
        # 回归基线：起跑前的干净副本（同一份 fixture，没被 agent 动过）
        baseline = None
        if task.get("regression"):
            with tempfile.TemporaryDirectory(prefix="aml-bench-base-") as base:
                prepare_workspace(task, base, fixtures)
                baseline = run_check(_as_cmd(task["regression"]), base)
        if arm in ("on", "ablate"):
            memory = retrieve(cfg, task, log=log)
            if arm == "ablate" and ablate > 0:
                # 反事实臂：把排在最前的 N 条记忆藏掉再跑 —— 直接回答"注入的记忆到底有没有被用上"
                dropped = memory["items"][:ablate]
                memory["items"] = memory["items"][ablate:]
                memory["hashes"] = [i["hash"] for i in memory["items"]]
                memory["text"] = "\n".join(i["text"] for i in memory["items"])
                memory["chars"] = sum(i["chars"] for i in memory["items"])
                memory["lines"] = len(memory["items"])
                memory["ablated"] = [i["hash"] for i in dropped]
        prompt = build_prompt(task, memory["text"] if arm in ("on", "ablate") else "")
        run = run_agent(agent, prompt, workdir, timeout=int(task.get("timeout", DEFAULT_TIMEOUT)),
                        env=env)
        after = snapshot(workdir)
        # 先取"agent 自己的产出"正文，再注入 _hidden/：否则验收标准自己的正文会污染判分
        agent_text = _workspace_text(workdir)
        # 判分前才把 `_hidden/` 放进来：验收标准不在场内，agent 想达标只能靠"记得"或"猜对"
        hidden = inject_hidden(task, workdir, fixtures)
        verify = run_check(_as_cmd(task.get("verify")), workdir)
        row = grade(task, run, workdir, before, after, baseline=baseline, verify=verify,
                    workspace_text=agent_text)
        row["hidden_files"] = hidden
        # `memory_used` 只在真的注入了记忆的组有意义：OFF 组没有注入，
        # 工作区里出现同名串纯属巧合（第一版没清，OFF 组也报"用了注入的经验"，指标直接失真）
        if arm not in ("on", "ablate"):
            row["memory_used"] = []
        row.update({"id": task["id"], "arm": arm, "query": task.get("query"),
                    "injected_lines": memory["lines"], "injected_chars": memory["chars"],
                    "injected_hashes": memory["hashes"], "memory_empty": memory["empty"],
                    "ablated_hashes": memory.get("ablated") or [],
                    "memory_items": memory.get("items") or [],
                    "workdir": workdir if keep else None})
        row.update({k: run.get(k) for k in ("ok", "error", "wall_ms", "turns", "duration_ms",
                                            "tokens_in", "tokens_out", "cache_read", "cost_usd",
                                            "denials", "stderr")})
        row["result_text"] = (run.get("result_text") or "")[:2000]
        return row
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)


def evaluate(cfg, tasks: list, arms=("off", "on"), agent: list | None = None,
             fixtures: str | None = None, keep: bool = False, repeats: int = 1,
             log=None, feedback: bool = False, ablate: int = 0) -> dict:
    """主线：任务 × 分组 × 重复次数，逐条打印进度（跑一次要花钱，必须看得见）。"""
    log = log or print
    agent = resolve_agent(agent or default_agent())
    arms = list(arms)
    if ablate > 0 and "ablate" not in arms:
        arms.append("ablate")
    rows = []
    total = len(tasks) * len(arms) * max(1, repeats)
    done = 0
    for task in tasks:
        for arm in arms:
            for rep in range(max(1, repeats)):
                done += 1
                label = {"on": "memory ON", "off": "memory OFF",
                         "ablate": f"memory ON − 前 {ablate} 条（反事实）"}.get(arm, arm)
                log(f"[{done}/{total}] {task['id']} · {label}"
                    + (f" · 第 {rep + 1} 次" if repeats > 1 else "") + " …")
                row = execute(cfg, task, arm, agent, fixtures=fixtures, keep=keep, log=log,
                              ablate=ablate)
                row["repeat"] = rep + 1
                rows.append(row)
                mark = "成功" if row["success"] else "失败"
                extra = ""
                if row["forbidden_hits"]:
                    extra += f" 违禁命中 {row['forbidden_hits']}"
                if row["regressed"]:
                    extra += " 回归!"
                log(f"    {mark}｜{row['wall_ms']}ms｜轮数 {row['turns']}｜"
                    f"改动 {len(row['changed_files'])} 文件｜注入 {row['injected_chars']} 字"
                    f"｜${row['cost_usd'] if row['cost_usd'] is not None else '?'}{extra}")
                if feedback and arm == "on":
                    row["feedback"] = post_feedback(cfg, row, log=log)
    report = summarize(rows, arms=list(arms))
    report["agent"] = agent
    return report


def post_feedback(cfg, row: dict, log=print) -> dict:
    """把一次运行的结果回写成记忆反馈（Step 4 的"自动反馈收集"入口）。

    归因口径（只有证据指向**这一条自己**时才敢下重话）：
      · 这条的正文里出现了「被 agent 真用上的做法」（expect_memory 命中）+ 任务成功 → `worked`
      · 这条的正文里出现了「任务声明的过期/错误做法」（forbidden 命中）→ `failed`
      · 其余被注入的 → `used`（"被用过"，**不主张**它有用）

    所以任务表里 `forbidden` / `expect_memory` 最好直接**摘那条记忆正文里的词**，
    归因才精确；摘不出来也不影响判分，只是反馈退化成运行级。
    """
    from . import feedback as fb
    items = row.get("memory_items") or []
    if not items:
        return {"recorded": 0}
    forbidden = row.get("forbidden_hits") or []
    used_marks = row.get("memory_used") or []
    verdicts = {}
    for item in items:
        body, h = item.get("text") or "", item.get("hash") or ""
        if not h:
            continue
        blamed = [f for f in forbidden if f in body]
        credited = [m for m in used_marks if m in body]
        if blamed:
            verdicts[h] = "failed"
        elif credited and row.get("success"):
            verdicts[h] = "worked"
        else:
            verdicts[h] = "used"
    if not verdicts:
        return {"recorded": 0}
    result = {"recorded": 0, "verdicts": verdicts}
    for h, outcome in verdicts.items():
        note = f"taskbench:{row['id']}:{row['arm']}"
        try:
            info = fb.record(cfg, h, outcome, note=note, log=lambda *_a, **_k: None)
        except Exception as e:  # noqa: BLE001 - 反馈失败不该让基准整体崩
            info = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        result["recorded"] += 1 if info.get("ok") else 0
    log(f"    反馈回写：{result['recorded']}/{len(verdicts)} 条（{sorted(set(verdicts.values()))}）")
    return result


# --------------------------------------------------------------------------- 汇总与渲染

def _avg(values):
    clean = [v for v in values if isinstance(v, (int, float))]
    return round(sum(clean) / len(clean), 1) if clean else None


def summarize(rows: list, arms=None) -> dict:
    """按分组汇总六个指标，并给出 ON − OFF 的差值（差值才是结论）。"""
    arms = arms or sorted({r["arm"] for r in rows})
    per_arm = {}
    for arm in arms:
        group = [r for r in rows if r["arm"] == arm]
        if not group:
            continue
        per_arm[arm] = {
            "runs": len(group),
            "success": sum(1 for r in group if r["success"]),
            "success_rate": round(sum(1 for r in group if r["success"]) / len(group), 3),
            "rework_turns_avg": _avg([r.get("turns") for r in group]),
            "changed_files_avg": _avg([len(r.get("changed_files") or []) for r in group]),
            "wall_ms_avg": _avg([r.get("wall_ms") for r in group]),
            "tokens_avg": _avg([(r.get("tokens_in") or 0) + (r.get("tokens_out") or 0)
                                if r.get("tokens_in") is not None else None for r in group]),
            "cost_usd_total": round(sum(r.get("cost_usd") or 0 for r in group), 4),
            "forbidden_runs": sum(1 for r in group if r.get("forbidden_hits")),
            "regressed_runs": sum(1 for r in group if r.get("regressed")),
            "injected_chars_avg": _avg([r.get("injected_chars") for r in group]),
            "injected_chars_pct": _pct([r.get("injected_chars") for r in group]),
            "memory_used_runs": sum(1 for r in group if r.get("memory_used")),
            "errors": sum(1 for r in group if not r.get("ok")),
        }
    delta = None
    if "on" in per_arm and "off" in per_arm:
        on, off = per_arm["on"], per_arm["off"]
        delta = {"success_rate": round(on["success_rate"] - off["success_rate"], 3),
                 "turns": _sub(on["rework_turns_avg"], off["rework_turns_avg"]),
                 "tokens": _sub(on["tokens_avg"], off["tokens_avg"]),
                 "wall_ms": _sub(on["wall_ms_avg"], off["wall_ms_avg"]),
                 "forbidden_runs": on["forbidden_runs"] - off["forbidden_runs"],
                 "regressed_runs": on["regressed_runs"] - off["regressed_runs"]}
    ablate_delta = None
    if "ablate" in per_arm and "on" in per_arm:
        # 反事实：藏掉前 N 条之后，成功率/成本有没有变化 —— 没变化说明那些记忆没被用上
        ab, on = per_arm["ablate"], per_arm["on"]
        ablate_delta = {"success_rate": round(ab["success_rate"] - on["success_rate"], 3),
                        "turns": _sub(ab["rework_turns_avg"], on["rework_turns_avg"]),
                        "tokens": _sub(ab["tokens_avg"], on["tokens_avg"])}
    return {"tasks": len({r["id"] for r in rows}), "runs": len(rows), "arms": per_arm,
            "delta": delta, "ablate_delta": ablate_delta, "rows": rows}


def _sub(a, b):
    return None if a is None or b is None else round(a - b, 1)


def _pct(values: list, points=(50, 95)) -> dict:
    clean = sorted(v for v in values if isinstance(v, (int, float)))
    if not clean:
        return {}
    out = {}
    for point in points:
        index = min(len(clean) - 1, max(0, int(round(point / 100 * (len(clean) - 1)))))
        out[f"p{point}"] = clean[index]
    out["max"] = clean[-1]
    return out


CAVEAT = ("口径提醒：这是**小样本任务级**对照（真跑 agent、真花钱），量的是"
          "「有没有做成 / 有没有被记忆带偏 / 有没有踩坏既有功能」，不是统计显著性。"
          "绝对值会被模型与任务难度带偏，**只看 ON − OFF 的差值**。")


def render(report: dict) -> str:
    """人看的报告：每个分组一行，最后给差值，并把坑写在结尾。"""
    lines = [f"任务级基准（{report['tasks']} 个任务 × {report['runs']} 次运行）",
             f"  agent：{' '.join(report.get('agent') or [])}"]
    head = (f"  {'分组':<6}{'成功率':>8}{'轮数':>7}{'改动文件':>9}{'耗时':>8}"
            f"{'token':>8}{'成本$':>9}{'违禁':>6}{'回归':>6}{'注入字':>8}{'P95':>7}")
    lines.append(head)
    order = [a for a in ("off", "on", "ablate") if a in report["arms"]]
    order += [a for a in sorted(report["arms"]) if a not in order]
    for arm in order:
        a = report["arms"][arm]
        pct = a.get("injected_chars_pct") or {}
        lines.append(f"  {arm.upper():<6}{a['success_rate']:>7.0%}{_fmt(a['rework_turns_avg']):>7}"
                     f"{_fmt(a['changed_files_avg']):>9}{_fmt(a['wall_ms_avg']):>8}"
                     f"{_fmt(a['tokens_avg']):>8}{a['cost_usd_total']:>9}"
                     f"{a['forbidden_runs']:>6}{a['regressed_runs']:>6}"
                     f"{_fmt(a['injected_chars_avg']):>8}{_fmt(pct.get('p95')):>7}")
    d = report.get("delta")
    if d:
        lines.append(f"  差值(ON−OFF)：成功率 {d['success_rate']:+.0%}｜轮数 {_sgn(d['turns'])}｜"
                     f"token {_sgn(d['tokens'])}｜耗时 {_sgn(d['wall_ms'])}ms｜"
                     f"违禁 {d['forbidden_runs']:+d}｜回归 {d['regressed_runs']:+d}")
    ad = report.get("ablate_delta")
    if ad:
        lines.append(f"  反事实（藏掉前 N 条 − 完整注入）：成功率 {ad['success_rate']:+.0%}｜"
                     f"轮数 {_sgn(ad['turns'])}｜token {_sgn(ad['tokens'])}"
                     "  → 都接近 0 说明这些记忆没改变结果（可能只是装饰）")
    used = report["arms"].get("on", {}).get("memory_used_runs", 0)
    if report["arms"].get("on"):
        lines.append(f"  ON 组有 {used}/{report['arms']['on']['runs']} 次运行里出现了"
                     f"「确实用了注入经验」的痕迹")
    bad = [(r["id"], r["arm"]) for r in report["rows"]
           if r.get("forbidden_hits") or r.get("regressed") or not r.get("ok")]
    if bad:
        lines.append("  需要人看一眼的：" + "、".join(f"{i}/{a}" for i, a in bad))
    lines.append("  " + CAVEAT)
    return "\n".join(lines)


def _fmt(value):
    return "?" if value is None else (f"{value:.0f}" if isinstance(value, float) else str(value))


def _sgn(value):
    return "?" if value is None else f"{value:+.1f}"


def default_task_path(cfg) -> str:
    return str(cfg.state_dir / "bench-tasks-task.jsonl")


def save_report(cfg, report: dict, when: str | None = None) -> str:
    """把完整报告落到 `state/bench/task-runs/`（含每一行的原始判分，便于复查与回滚判断）。

    注入的记忆正文**不落盘**（只留 hash 与字数）—— 报告可能被分享，正文里往往带项目细节。
    """
    stamp = when or time.strftime("%Y%m%d-%H%M%S")
    out_dir = cfg.state_dir / "bench" / "task-runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"task-bench-{stamp}.json"
    slim = dict(report)
    slim["rows"] = [{k: v for k, v in row.items() if k != "memory_items"} for row in report["rows"]]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(slim, f, ensure_ascii=False, indent=2, default=str)
    return str(path)
