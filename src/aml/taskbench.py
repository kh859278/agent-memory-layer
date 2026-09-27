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
# 重试时喂回去的验收输出上限：够看清失败原因，又不至于把上下文塞爆
MAX_FEEDBACK_CHARS = 4000

# 重试时喂回去什么（任务表 `feedback` 字段）：
#   hidden —— 隐藏验收的完整输出（老行为）。**会泄题**：判分器的话里常点名规则本身。
#   public —— 只跑工作区里本来就有、agent 自己也能跑的公开检查，喂它的输出。
#   none   —— 只告诉它"没过"，不给任何外部信息；纯靠自己返工。
#
# 为什么要有后两档 —— 2026-09-26 实测（sandbox-tmp，三臂对照）：
# 三轮那组把判分器的话喂回去之后，OFF 组收尾原话是
# 「`nomkdtemp` 的根因是**文档字符串本身**」—— 它学会的是"别让这个字符串出现"，
# 而不是那条经验。喂回去的是判分标准，量到的就是"照抄判分标准"的能力。
# 结论：**要量"返工能力"，喂回去的信息就不能泄题**；要量"照抄能力"，才用 hidden。
FEEDBACK_MODES = ("hidden", "public", "none")
FEEDBACK_DEFAULT = "hidden"


def normalize_feedback(value) -> str:
    """把任务表里的 `feedback` 归一成合法档位。

    写错的档位**回落到 hidden**（老行为），不回落成"不喂" ——
    "不喂"会把返工能力测成运气，而且没人看得出来是配置写错了。
    """
    mode = str(value or FEEDBACK_DEFAULT).strip().lower()
    return mode if mode in FEEDBACK_MODES else FEEDBACK_DEFAULT


# `forbidden` 查哪儿：
#   probe（默认）—— agent 自述 + 验收输出 + 工作区正文。老行为。
#   artifacts    —— **只查工作区里交出来的东西**。
#
# 为什么要这一档（2026-09-26 实测）：journal 组的 ON 交出了 7/7 全对的一篇，
# 却在收尾里写了一句「其他｜未写校内指导老师、未附参考资料」——
# 而 `forbidden` 是子串判据，于是它**声明自己没写**反被判成写了，成功被抹成失败。
# 这跟"声明自己没写被记成写了"是同一个毛病：判据该查**产物**，不该查 agent 的自述。
FORBIDDEN_SCOPES = ("probe", "artifacts")
FORBIDDEN_SCOPE_DEFAULT = "probe"


def normalize_forbidden_in(value) -> str:
    scope = str(value or FORBIDDEN_SCOPE_DEFAULT).strip().lower()
    return scope if scope in FORBIDDEN_SCOPES else FORBIDDEN_SCOPE_DEFAULT

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
        # 最多跑几次。1 = 只跑一次（老行为）；>1 时验收失败会把输出喂回去重跑
        task.setdefault("max_rounds", 1)
        # 重试时**喂回去什么**：见 `feedback_for`。默认 hidden = 老行为。
        task["feedback"] = normalize_feedback(task.get("feedback"))
        # 泄题词：喂回去的正文里一旦出现这些词，就说明这一轮等于把答案给了 agent
        task.setdefault("leak_words", [])
        # `forbidden` 查哪儿：probe（默认，含 agent 的自述）/ artifacts（只查工作区产物）
        task["forbidden_in"] = normalize_forbidden_in(task.get("forbidden_in"))
        # 命令统一归一成数组：字符串里带空格的路径不会被 shlex 拆坏
        task["verify"] = _as_cmd(task["verify"])
        if task.get("regression"):
            task["regression"] = _as_cmd(task["regression"])
        if task.get("public"):
            task["public"] = _as_cmd(task["public"])
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


def _remove_hidden(root: str, rels) -> None:
    """把判分前拷进去的 `_hidden/` 再撤走。

    为什么必须撤：重试时 agent 在**同一个工作区**里继续干活，
    隐藏验收留在那儿就等于把答案摊开给它看。
    sde-bench 把这叫 no answer leakage —— 是重试机制能成立的前提。
    """
    for rel in rels or []:
        path = os.path.join(root, rel)
        try:
            os.remove(path)
        except OSError:
            pass
        parent = os.path.dirname(path)
        while parent and os.path.abspath(parent) != os.path.abspath(root):
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)


def parse_summary(output: str) -> dict:
    """把验收脚本自己打的 `SUMMARY {...}` 抠出来 —— 部分分。

    为什么必须抠：判分器早就在打结构化结果（`core_pass/core_total` 等），
    但基准一直只记 `rc`（二值）。于是"核心判据过 6/7 还是 0/7"这种**有分辨率的差别
    全被丢掉**，只剩"过没过"。2026-09-24 那次三组配套实测就是这样被埋掉的：
    OFF 核心 0/7、2/7、0/7，ON 核心 7/7、6/7、7/7 —— 差 6 项，二值上却只差 1 组。

    约定：SUMMARY 行必须是**最后一个**能解析出 JSON 的 `SUMMARY ` 前缀行；
    解析不了就返回 {}（老判分器没有 SUMMARY，不能因此让整轮作废）。
    """
    found = {}
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("SUMMARY "):
            continue
        try:
            data = json.loads(line[len("SUMMARY "):])
        except ValueError:
            continue
        if isinstance(data, dict):
            found = data
    return found


def checks_from(verify: dict) -> dict:
    """从验收输出里取部分分，并补一个 `core_rate`（0–1，方便直接比大小）。"""
    raw = parse_summary((verify or {}).get("output") or "")
    out = dict(raw)
    total = out.get("core_total")
    if isinstance(total, int) and total > 0:
        out["core_rate"] = round(int(out.get("core_pass") or 0) / total, 3)
    elif raw:
        # 判分器只打了别的结构（比如只有 pass/total）：也算得出比率
        total = raw.get("total")
        if isinstance(total, int) and total > 0:
            out["core_rate"] = round(int(raw.get("pass") or 0) / total, 3)
    return out


def leak_words_hit(task: dict, text: str) -> list:
    """喂回去的正文里出现了任务声明的泄题词 → 这一轮等于把答案递给了 agent。

    任务表 `leak_words` 就是"判分器一旦说出这个词，题目就作废"的那些词
    （对 sandbox-tmp 而言是 `0o700`／`chmod`：说了等于告诉它答案）。
    有了这个守卫，"不泄题"才是个**可核验的**性质，而不是我口头保证的。
    """
    return [w for w in (task.get("leak_words") or []) if w and w in (text or "")]


def feedback_for(task: dict, verify_output: str, public_output: str | None) -> dict:
    """按 `feedback` 档位决定重试时喂回去的正文，并顺手查有没有泄题。

    返回 `{mode, source, text, leaks}`：
      · mode=hidden → 隐藏验收输出（可能泄题，leaks 如实记录）
      · mode=public → 只看公开检查的输出；若这一轮没跑公开检查，就退化成"只说没过"
      · mode=none   → 只说没过
    泄题时**仍然照原样喂**（不改历史行为），但把 `leaks` 记进结果行 ——
    这样"这次差分可能是抄来的"会被数据自己标出来，而不是靠我记得。
    """
    mode = normalize_feedback(task.get("feedback"))
    if mode == "hidden":
        text, source = (verify_output or ""), "hidden"
    elif mode == "public":
        text, source = (public_output or ""), ("public" if public_output else "none")
    else:
        text, source = "", "none"
    if len(text) > MAX_FEEDBACK_CHARS:
        text = text[-MAX_FEEDBACK_CHARS:]
    return {"mode": mode, "source": source, "text": text,
            "leaks": leak_words_hit(task, text)}


def _run_in_copy(cmd, workdir: str, timeout: int = 300) -> dict:
    """在**工作区副本**里跑公开检查。

    为什么不直接在工作区里跑：公开检查自己会写临时文件/缓存，跑完留在地上，
    下一轮的 `changed_files` 与正文扫描（forbidden / expect_memory 的 probe）
    就会把这些噪音算成 agent 的产出。挪到副本里跑，主工作区一行不动。
    """
    with tempfile.TemporaryDirectory(prefix="aml-bench-pub-") as tmp:
        target = os.path.join(tmp, "w")
        try:
            shutil.copytree(workdir, target,
                            ignore=shutil.ignore_patterns(*sorted(SKIP_DIRS)))
        except OSError as e:
            return {"cmd": cmd, "rc": 127, "output": f"公开检查跑不起来（复制工作区失败）：{e}",
                    "skipped": False}
        return run_check(cmd, target, timeout=timeout)


def _retry_prompt(task: dict, memory_block: str, feedback: str, attempt: int) -> str:
    """重试用的 prompt：原任务 + 上一轮验收输出原文（像评审把失败结果拍回来）。"""
    head = build_prompt(task, memory_block)
    tail = (feedback or "").strip()
    if len(tail) > MAX_FEEDBACK_CHARS:
        tail = tail[-MAX_FEEDBACK_CHARS:]
    note = f"【第 {attempt} 轮验收没通过】你上一轮的改动还留在工作区里。"
    if tail:
        return f"{head}\n\n{note}下面是验收命令的输出原文，照它修，别整份重写：\n{tail}"
    return f"{head}\n\n{note}请据此修正，别整份重写。"


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
    # `forbidden` 默认查 probe（含 agent 自述），任务可收紧成只查产物 —— 见 normalize_forbidden_in
    scope = normalize_forbidden_in(task.get("forbidden_in"))
    forbidden_text = probe if scope == "probe" else agent_text
    forbidden = [f for f in (task.get("forbidden") or []) if f in forbidden_text]
    memory_used = [m for m in (task.get("expect_memory") or []) if m in probe]
    changed = changed_files(before, after)
    succeeded = verify.get("rc") == 0 and not missing and not forbidden
    # 回归：起跑前能过、跑完挂了 —— 这才是"踩坏既有功能"；起跑前就挂的不算账
    regressed = bool(regression and baseline and baseline.get("rc") == 0
                     and regression.get("rc") != 0)
    return {
        "verify_rc": verify.get("rc"), "verify_output": verify.get("output"),
        "missing_expect": missing, "forbidden_hits": forbidden,
        "forbidden_in": scope,
        "memory_used": memory_used, "changed_files": changed,
        "success": succeeded, "regressed": regressed,
        "checks": checks_from(verify),
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
        max_rounds = max(1, int(task.get("max_rounds", 1)))
        memory_block = memory["text"] if arm in ("on", "ablate") else ""
        prompt = build_prompt(task, memory_block)
        attempts = []
        feedback_log = []
        hidden = []
        verify = {"cmd": None, "rc": None, "output": "", "skipped": True}
        row = None
        last_run = {}
        for attempt in range(1, max_rounds + 1):
            if attempt > 1:
                log(f"    第 {attempt} 轮 · 上一轮验收没过，把输出原文喂回去")
            run = run_agent(agent, prompt, workdir,
                            timeout=int(task.get("timeout", DEFAULT_TIMEOUT)), env=env)
            after = snapshot(workdir)
            # 先取"agent 自己的产出"正文，再注入 _hidden/：否则验收标准自己的正文会污染判分
            agent_text = _workspace_text(workdir)
            # 判分前才把 `_hidden/` 放进来：验收标准不在场内，agent 想达标只能靠"记得"或"猜对"
            hidden = inject_hidden(task, workdir, fixtures)
            verify = run_check(_as_cmd(task.get("verify")), workdir)
            row = grade(task, run, workdir, before, after, baseline=baseline, verify=verify,
                        workspace_text=agent_text)
            attempts.append({"attempt": attempt, "success": row["success"],
                             "verify_rc": row.get("verify_rc"), "ok": run.get("ok"),
                             "error": run.get("error"), "turns": run.get("turns"),
                             "wall_ms": run.get("wall_ms"), "duration_ms": run.get("duration_ms"),
                             "tokens_in": run.get("tokens_in"), "tokens_out": run.get("tokens_out"),
                             "cache_read": run.get("cache_read"), "cost_usd": run.get("cost_usd")})
            # 判分完立刻撤走隐藏验收：留着的话，下一轮 agent 就直接看到了验收标准
            _remove_hidden(workdir, hidden)
            last_run = run
            if row["success"]:
                break
            if attempt < max_rounds:
                # 喂回去什么，按任务的 `feedback` 档位定；泄题词如实记进 feedback_log
                public_output = None
                mode = normalize_feedback(task.get("feedback"))
                if mode == "public" and task.get("public"):
                    public_output = _run_in_copy(
                        _as_cmd(task["public"]), workdir,
                        timeout=int(task.get("timeout", DEFAULT_TIMEOUT))).get("output") or ""
                fb = feedback_for(task, verify.get("output") or "", public_output)
                feedback_log.append({"attempt": attempt, "mode": fb["mode"],
                                     "source": fb["source"], "chars": len(fb["text"]),
                                     "leaks": fb["leaks"]})
                if fb["leaks"]:
                    log(f"    ⚠ 第 {attempt} 轮喂回去的正文里有泄题词 {fb['leaks']}"
                        f" —— 这一轮的重试不算「独立返工」")
                prompt = _retry_prompt(task, memory_block, fb["text"], attempt)

        # 纠正次数：成功前失败了几次（0 = 一次就对）；始终没成就等于每轮都错
        corrections = (len(attempts) - 1) if row["success"] else len(attempts)

        def _total(key):
            vals = [a.get(key) for a in attempts]
            return None if any(v is None for v in vals) else sum(vals)

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
                    "workdir": workdir if keep else None,
                    "max_rounds": max_rounds, "attempts": len(attempts),
                    "corrections": corrections, "attempts_detail": attempts,
                    "feedback_mode": normalize_feedback(task.get("feedback")),
                    "forbidden_in": normalize_forbidden_in(task.get("forbidden_in")),
                    "feedback_log": feedback_log,
                    "feedback_leaks": sorted({w for f in feedback_log for w in f["leaks"]}),
                    "checks": row.get("checks") or {}})
        row.update({k: _total(k) for k in ("wall_ms", "turns", "duration_ms",
                                           "tokens_in", "tokens_out", "cache_read")})
        costs = [a.get("cost_usd") for a in attempts]
        row["cost_usd"] = None if any(c is None for c in costs) else round(sum(costs), 6)
        row.update({k: last_run.get(k) for k in ("ok", "error", "denials", "stderr")})
        row["result_text"] = (last_run.get("result_text") or "")[:2000]
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


def _avg3(values):
    """部分分用的均值：保留三位小数（0.667 和 0.714 的差别不该被四舍五入吃掉）。"""
    clean = [v for v in values if isinstance(v, (int, float))]
    return round(sum(clean) / len(clean), 3) if clean else None


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
            # 核心判据通过率（部分分）：把判分器 SUMMARY 里的 core_pass/core_total 平均。
            # 为什么单列一项：二值成功率在难任务上会**饱和**——两组都 0，差别看不见；
            # 而"核心过 0/7 还是 6/7"是有分辨率的（2026-09-24 三组配套实测，差 6 项）。
            "core_rate_avg": _avg3([(r.get("checks") or {}).get("core_rate") for r in group]),
            "core_pass_total": sum(int((r.get("checks") or {}).get("core_pass") or 0)
                                   for r in group),
            "core_total_total": sum(int((r.get("checks") or {}).get("core_total") or 0)
                                    for r in group),
            "feedback_leak_runs": sum(1 for r in group if r.get("feedback_leaks")),
            # 纠正次数：0 = 一次就对。比"成功/失败"细，才看得出记忆有没有减少返工
            "corrections_avg": _avg([r.get("corrections") for r in group]),
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
                 "core_rate": _sub3(on.get("core_rate_avg"), off.get("core_rate_avg")),
                 "turns": _sub(on["rework_turns_avg"], off["rework_turns_avg"]),
                 "tokens": _sub(on["tokens_avg"], off["tokens_avg"]),
                 "wall_ms": _sub(on["wall_ms_avg"], off["wall_ms_avg"]),
                 "corrections": _sub(on.get("corrections_avg"), off.get("corrections_avg")),
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


def _sub3(a, b):
    """部分分/比率的差值：保留三位小数。

    `_sub` 只留一位小数 —— 用在 0.857 这种比率上会把 6/7 的差距压成 0.9，
    再小一点就直接抹成 0，等于又走回"看不见差别"的老路。
    """
    return None if a is None or b is None else round(a - b, 3)


def _sgn3(value):
    return "?" if value is None else f"{value:+.3f}"


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
    head = (f"  {'分组':<6}{'成功率':>8}{'核心判据':>14}{'纠正':>7}{'轮数':>7}{'改动文件':>9}"
            f"{'耗时':>8}{'token':>8}{'成本$':>9}{'违禁':>6}{'回归':>6}{'注入字':>8}{'P95':>7}")
    lines.append(head)
    order = [a for a in ("off", "on", "ablate") if a in report["arms"]]
    order += [a for a in sorted(report["arms"]) if a not in order]
    for arm in order:
        a = report["arms"][arm]
        pct = a.get("injected_chars_pct") or {}
        core = a.get("core_rate_avg")
        core_s = "—" if core is None else f"{core:.0%}"
        if a.get("core_total_total"):
            core_s += f"({a['core_pass_total']}/{a['core_total_total']})"
        lines.append(f"  {arm.upper():<6}{a['success_rate']:>7.0%}{core_s:>14}"
                     f"{_fmt(a.get('corrections_avg')):>7}{_fmt(a['rework_turns_avg']):>7}"
                     f"{_fmt(a['changed_files_avg']):>9}{_fmt(a['wall_ms_avg']):>8}"
                     f"{_fmt(a['tokens_avg']):>8}{a['cost_usd_total']:>9}"
                     f"{a['forbidden_runs']:>6}{a['regressed_runs']:>6}"
                     f"{_fmt(a['injected_chars_avg']):>8}{_fmt(pct.get('p95')):>7}")
    d = report.get("delta")
    if d:
        lines.append(f"  差值(ON−OFF)：成功率 {d['success_rate']:+.0%}｜"
                     f"核心判据 {_sgn3(d.get('core_rate'))}｜"
                     f"纠正 {_sgn(d.get('corrections'))}｜轮数 {_sgn(d['turns'])}｜"
                     f"token {_sgn(d['tokens'])}｜耗时 {_sgn(d['wall_ms'])}ms｜"
                     f"违禁 {d['forbidden_runs']:+d}｜回归 {d['regressed_runs']:+d}")
        if d.get("core_rate") is not None and abs(d["core_rate"]) >= 0.5:
            lines.append("  ↑ 核心判据差值 ≥0.5：二值成功率可能已经饱和，"
                         "**以核心判据为准**（这才是那个有分辨率的指标）")
    leaks = {a: v.get("feedback_leak_runs", 0) for a, v in report["arms"].items()}
    if any(leaks.values()):
        lines.append(f"  ⚠ 重试喂回去的正文里有泄题词的运行：{leaks}"
                     " —— 这些运行的「纠正次数」不能当作独立返工能力")
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
