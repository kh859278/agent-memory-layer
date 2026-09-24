"""蒸馏：把一段会话变成**可跨项目复用**的知识。

分工（这套分层是原系统跑了几百条沉淀之后定下来的）：
  · `kind:knowledge` + `reusable:true` + `domain:<领域>` —— 跨项目可复用，**不打 project**
  · `project:<名>` —— 只对当前项目成立的事实与流水
  · 每条知识带 `review_after`：pitfall/tooling 180 天、pattern/checklist 365 天、decision 730 天
    （技术类结论会过期，方法论相对稳定；到期由 `aml review` 提醒）

输入控制是踩过坑的：喂给模型的文本**必须截断 + 总量封顶**，
否则推理会吃光输出预算、模型返回空内容（实测出现过 1.2 万 tokens 换 0 字）。

**发送前必须脱敏**（2026-09-22 加，外部评审点出来的第二条）：蒸馏是本程序里**唯一**
把内容送出本机的动作，而送出去的正是会话原文——用户消息里出现过的密钥、内网地址、
客户名都会跟着走。所以 `call_llm()` 在发请求前统一过一遍 `redact`（规则与提交前的
内容泄漏扫描**同一份**，见 `aml/redact.py`），并把"盖掉了几处"记进 usage，
由 `drain()` 打给人看。默认开；要关得显式改配置（`distill.redact: false`）。

API key 也从 2026-09-22 起**只认显式来源**（环境变量 / 配置），
不再默认去翻 `~/.dsh/.credentials.yaml` —— 从别人的凭据文件里默默抠 key 太隐晦，
要保留那条路必须显式写 `distill.allow_dsh_credentials: true`。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.request

from .http import MemoryClient, client_for
from .redact import redact
from .text import write_lf

PROMPT = """下面是某个项目的**一段真实工作会话记录**（按时间排序，U=用户任务，A=agent 回复）。

请提炼其中【可跨项目复用】的知识：方法论、踩过的坑、工具用法、环境配置、决策理由。
**不要**收录只对本项目成立的一次性事实（具体客户名、具体链接、某天的进度数字）、寒暄、以及没有信息量的短句。

输出**严格的 JSON 数组**，每个元素字段：
{"title":"≤20字标题","domain":"kebab-case领域如 env-windows/data-scraping/agent-workflow",
 "type":"pitfall|pattern|decision|tooling|checklist","body":"≤300字，自包含，脱离本项目也能看懂",
 "evidence":"简短来源线索","confidence":"high|medium|low"}

要求：宁少勿滥（0–8 条）；用你自己组织的语言，不要整段复制；只输出 JSON，不要任何解释或代码块标记。
若这段会话没有可复用知识，输出 [].

会话记录：
"""

REVIEW_DAYS = {"pitfall": 180, "tooling": 180, "pattern": 365, "checklist": 365, "decision": 730}
DEFAULT_REVIEW_DAYS = 365


def review_after_for(ktype: str, base: dt.datetime | None = None) -> str:
    days = REVIEW_DAYS.get((ktype or "").strip(), DEFAULT_REVIEW_DAYS)
    return ((base or dt.datetime.now()) + dt.timedelta(days=days)).strftime("%Y-%m-%d")


# ------------------------------------------------------------------- 队列

class Queue:
    def __init__(self, cfg):
        self.path = cfg.state_dir / "distill_queue.json"
        self.data = {"pending": [], "done": []}
        if self.path.is_file():
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data["pending"] = loaded.get("pending") or []
                self.data["done"] = loaded.get("done") or []
            except (OSError, ValueError):
                pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def enqueue(self, session_id: str, workspace: str = "", agent: str | None = None,
                mem_count: int | None = None, min_tasks: int = 0, reason: str = "") -> bool:
        """入队（去重；已蒸过的会话内容涨得够多才允许重蒸）。返回是否新入队。"""
        if any(x.get("session_id") == session_id for x in self.data["pending"]):
            return False
        old = next((x for x in self.data["done"] if x.get("session_id") == session_id), None)
        incr_min = int(os.environ.get("DISTILL_INCR_MIN", "5"))
        incr_ratio = float(os.environ.get("DISTILL_INCR_RATIO", "1.5"))
        if old is not None:
            was = old.get("mem_count")
            if not (isinstance(was, int) and was > 0 and isinstance(mem_count, int)):
                return False
            if mem_count >= was + incr_min and mem_count >= was * incr_ratio:
                item = dict(old)
                item.update({"workspace": workspace or old.get("workspace"),
                             "mem_count": mem_count, "reason": f"增量重蒸（{was}→{mem_count} 条）",
                             "requeued_at": dt.datetime.now().isoformat(timespec="seconds")})
                self.data["done"] = [x for x in self.data["done"] if x.get("session_id") != session_id]
                self.data["pending"].append(item)
                self.save()
                return True
            return False
        item = {"session_id": session_id, "short": session_id.replace("session-", "")[:8],
                "workspace": workspace, "queued_at": dt.datetime.now().isoformat(timespec="seconds"),
                "min_tasks": min_tasks, "reason": reason}
        if agent:
            item["agent"] = agent
        if isinstance(mem_count, int):
            item["mem_count"] = mem_count
        self.data["pending"].append(item)
        self.save()
        return True


_lock = None


def acquire_lock(cfg, stale_sec: int = 7200) -> bool:
    """互斥锁：兜底任务与手动补蒸并发跑同一份 pending 会把同一会话蒸两遍。"""
    global _lock
    path = cfg.state_dir / "distill.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        _lock = path
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(path) > stale_sec:
                os.remove(path)
                return acquire_lock(cfg, stale_sec)
        except OSError:
            pass
        return False
    except OSError:
        return True


def release_lock():
    global _lock
    if _lock:
        try:
            os.remove(_lock)
        except OSError:
            pass
        _lock = None


# --------------------------------------------------------------- 模型调用

def api_key(cfg) -> str:
    """取 API key：**只认显式来源**。

    顺序：`DISTILL_API_KEY` 环境变量 → 配置 `distill.api_key` → （仅在显式开启时）
    `distill.credentials_file` 指向的文件。

    为什么默认不再读 DSH 的凭据文件（2026-09-22 改）：那是一个"用户不知道自己在被读"的
    隐式路径 —— 一个记忆工具悄悄从别的 agent 的凭据文件里抠 key，不符合最小惊讶原则。
    老行为保留但必须显式：`distill.allow_dsh_credentials: true`。
    """
    key = os.environ.get("DISTILL_API_KEY")
    if key:
        return key
    spec = cfg.section("distill")
    configured = str(spec.get("api_key") or "").strip()
    if configured:
        return configured
    if not spec.get("allow_dsh_credentials"):
        return ""
    cred = spec.get("credentials_file") or "~/.dsh/.credentials.yaml"
    try:
        with open(os.path.expanduser(cred), encoding="utf-8") as f:
            m = re.search(r"DEEPSEEK_API_KEY:\s*['\"]?([^'\"\s]+)", f.read())
            return m.group(1) if m else ""
    except OSError:
        return ""


def safe_transcript(cfg, transcript: str) -> tuple:
    """发送前脱敏：返回 `(文本, 命中标签, 盖掉几处)`。

    开关是 `distill.redact`（**默认开**）；自定义屏蔽词走 `distill.redact_words`
    （客户名/项目名这类没有正则形态的东西）。规则表与 `tools/scrub_check.py` 共用一份。
    """
    spec = cfg.section("distill")
    if not spec.get("redact", True):
        return transcript, [], 0
    out, labels = redact(transcript, extra_words=spec.get("redact_words") or [])
    return out, labels, out.count("［已脱敏］")


def build_transcript(mems, max_chars: int = 8000, per_mem: int = 200, max_mem: int = 400):
    """拼喂给模型的文本：逐条截断 + 总量封顶（超长时均匀抽样，保住时间跨度）。"""
    lines = [f"[{dt.datetime.fromtimestamp(ts, dt.timezone.utc):%m-%d %H:%M}] "
             f"{'U' if kind == 'task' else 'A'}：{content[:per_mem]}"
             for ts, content, kind in mems[:max_mem]]
    text = "\n\n".join(lines)
    if len(text) <= max_chars:
        return text, len(lines)
    keep = max(6, int(len(lines) * max_chars / max(len(text), 1)))
    step = len(lines) / keep
    picked = [lines[int(i * step)] for i in range(keep)]
    return "\n\n".join(picked)[:max_chars], len(picked)


def call_llm(cfg, transcript: str, model: str | None = None, key: str | None = None):
    spec = cfg.section("distill")
    key = key or api_key(cfg)
    if not key:
        raise RuntimeError("没有可用的 API key：设置 DISTILL_API_KEY、或在配置里写 "
                           "distill.api_key（想沿用老的「读 DSH 凭据文件」行为，"
                           "要显式打开 distill.allow_dsh_credentials）")
    # 唯一的出网点，脱敏就放在这里：任何调用方都绕不过去（重试那一发也走这条）
    payload_text, labels, redacted = safe_transcript(cfg, transcript)
    body = json.dumps({
        "model": model or spec.get("model", "deepseek-flash"),
        "messages": [{"role": "user", "content": PROMPT + payload_text}],
        "temperature": 0.3,
        "max_tokens": int(spec.get("max_out_tokens", 6000)),
    }).encode()
    req = urllib.request.Request(str(spec.get("base_url", "https://api.deepseek.com")).rstrip("/")
                                 + "/chat/completions",
                                 data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as f:
        payload = json.loads(f.read())
    usage = payload.get("usage") or {}
    # 把脱敏情况捎回给调用方（usage 是这次调用新建的 dict）
    usage["_redacted"] = redacted
    usage["_redacted_labels"] = labels
    return payload["choices"][0]["message"]["content"], usage


def parse_entries(text: str) -> list:
    """模型爱包代码块、爱加解释：这里只揪最外层的 JSON 数组。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("["), t.rfind("]")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    try:
        data = json.loads(t)
        return data if isinstance(data, list) else []
    except ValueError:
        return []


# ----------------------------------------------------------------- 落盘

def sink_markdown(cfg, entries, item) -> int:
    """同时落一份人能读的 markdown（库里有、文件里也有，谁都不锁死谁）。"""
    sink = cfg.knowledge_dir / str(cfg.section("distill").get("sink_subdir", "沉淀"))
    sink.mkdir(parents=True, exist_ok=True)
    written = 0
    for e in entries:
        if not isinstance(e, dict) or not e.get("body"):
            continue
        domain = (e.get("domain") or "general").strip() or "general"
        path = sink / f"{domain}.md"
        try:
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(f"\n## {e.get('title', '')}\n\n")
                f.write(f"- 类型：{e.get('type')} ｜ 可信度：{e.get('confidence')} "
                        f"｜ 来源：{item.get('agent') or 'agent'} 会话 {item.get('short')}"
                        f"（{item.get('workspace') or '未知项目'}）"
                        f" ｜ 复核：{review_after_for(e.get('type'))}\n")
                f.write(f"- 线索：{e.get('evidence', '')}\n\n")
                f.write(f"{e['body']}\n")
            written += 1
        except OSError:
            pass
    return written


def session_key(session_id: str) -> str:
    """会话的**规范短键**：去掉 `session-` 前缀后取前 8 位。

    必须和 `ingest` 写进标签的那个键对齐（`session:<前8位>`），否则"知识 → 原会话"join 不上。

    为什么会有这个函数（2026-09-24 审计）：`src_session` 历史上写进去过两种形态 ——
    带 `session-` 前缀的 36 位全 id（1 432 条）与纯 8 位短 hex（16 条）。
    直接按字符串比对时，join 成功率看起来只有 2%；**归一到核心 8 位后其实是 99.7%**。
    也就是说这条链没断，只是没归一。写入侧统一用它，读取侧比较前也要归一。
    """
    s = str(session_id or "")
    if s.startswith("session-"):
        s = s[len("session-"):]
    return s[:8]


def write_entries(cfg, entries, item, client: MemoryClient | None = None) -> int:
    """写回记忆层：kind:knowledge + domain + 复核期。"""
    client = client or client_for(cfg)
    ok = 0
    key = session_key(item.get("session_id") or item.get("short"))
    full_id = str(item.get("session_id") or "")
    for e in entries:
        if not isinstance(e, dict) or not e.get("body"):
            continue
        domain = (e.get("domain") or "general").strip() or "general"
        ktype = (e.get("type") or "pattern").strip() or "pattern"
        content = f"【{e.get('title', '')}】{e['body']}"
        tags = ["kind:knowledge", "reusable:true", f"domain:{domain}", f"ktype:{ktype}",
                f"confidence:{(e.get('confidence') or 'medium')}",
                f"src_session:{key}"]
        metadata = {"title": e.get("title", ""), "domain": domain, "ktype": ktype,
                    "evidence": e.get("evidence", ""), "src_session": key,
                    # 全 id 另存一份：短键用于和标签 join，全 id 用于精确回溯（两者都要有）
                    "src_session_id": full_id, "src_agent": item.get("agent", ""),
                    "src": "distill", "review_after": review_after_for(ktype)}
        try:
            res = client.store(content, tags, metadata, conversation_id=f"distill:{item['session_id']}")
            ok += 1 if res.get("success") else 0
        except Exception:  # noqa: BLE001
            pass
    return ok


# ----------------------------------------------------------------- 主流程

def session_memories(cfg, short: str, agent: str | None = None, client: MemoryClient | None = None):
    """取某会话已入库的记忆（按时间排序）。"""
    client = client or client_for(cfg)
    tags = [f"session:{short}"]
    if agent:
        tags.append(f"agent:{agent}")
    try:
        raw = client._request("/api/search/by-tag", {"tags": tags, "match_all": bool(agent),
                                                     "limit": 500})
    except Exception:  # noqa: BLE001
        return []
    out = []
    for x in (raw.get("results") or []):
        m = x.get("memory", x)
        out.append((m.get("created_at") or 0, (m.get("content") or "").strip(),
                    "task" if "kind:task" in (m.get("tags") or []) else "reply"))
    out.sort()
    return out


def drain(cfg, session_id: str | None = None, log=print, client: MemoryClient | None = None,
          call=None) -> dict:
    """清空队列（或只蒸指定会话）。逐会话落盘，中途中断不会重蒸已完成的部分。"""
    queue = Queue(cfg)
    if session_id:
        queue.data["pending"] = [x for x in queue.data["pending"] if x.get("session_id") == session_id]
    targets = list(queue.data["pending"])
    result = {"targets": len(targets), "done": 0, "entries": 0, "skipped": 0, "failed": 0}
    if not targets:
        log("队列为空")
        return result
    if not acquire_lock(cfg):
        log("已有蒸馏进程在跑（拿不到锁），退出")
        result["error"] = "locked"
        return result
    client = client or client_for(cfg)
    spec = cfg.section("distill")
    try:
        for item in targets:
            short = item.get("short") or item["session_id"].replace("session-", "")[:8]
            mems = session_memories(cfg, short, item.get("agent"), client)
            tasks = sum(1 for _, _, k in mems if k == "task")
            if not mems or tasks < int(item.get("min_tasks", 0)):
                log(f"  跳过 {short}：记忆 {len(mems)} 条 / 任务 {tasks} 轮（不足 {item.get('min_tasks', 0)}）")
                result["skipped"] += 1
            else:
                transcript, used = build_transcript(mems, int(spec.get("max_input_chars", 8000)))
                log(f"  蒸馏 {short}：喂入 {len(mems)} 条（{len(transcript)} 字）")
                try:
                    text, usage = (call or call_llm)(cfg, transcript)
                    entries = parse_entries(text)
                    if not entries and len(transcript) > int(spec.get("min_chars_for_retry", 3000)):
                        log("    0 条产出，换备用模型重试一次")
                        text, usage = (call or call_llm)(cfg, transcript,
                                                         model=spec.get("retry_model"))
                        entries = parse_entries(text)
                    written = write_entries(cfg, entries, item, client)
                    sink_markdown(cfg, entries, item)
                    result["entries"] += written
                    log(f"    提炼 {len(entries)} 条，写入 {written} 条"
                        f"（tokens {usage.get('total_tokens', '?')}）")
                    if usage.get("_redacted"):
                        log(f"    发送前已脱敏 {usage['_redacted']} 处"
                            f"（{'、'.join(usage.get('_redacted_labels') or [])}）"
                            "—— 规则与提交前的泄漏扫描同一份")
                except Exception as e:  # noqa: BLE001
                    result["failed"] += 1
                    log(f"    ⚠ 失败：{type(e).__name__} {str(e)[:120]}")
                    continue
            item = dict(item, distilled_at=dt.datetime.now().isoformat(timespec="seconds"),
                        mem_count=len(mems))
            queue.data["pending"] = [x for x in queue.data["pending"]
                                     if x.get("session_id") != item["session_id"]]
            queue.data["done"].append(item)
            queue.save()
            result["done"] += 1
    finally:
        release_lock()
    return result


def _knowledge_items(client) -> tuple:
    """取全部 `kind:knowledge` 条目，返回 `(items, error)`。

    **为什么不用 `search_by_tag(n=1000)`**：那是硬上限。库里一旦超过 1000 条，
    人面镜像就会**静默少掉后面的条目**（2026-09-24 实测：库内 1 123 条，
    重建结果只报 `entries: 1000`，少 123 条 = 11%）。所以优先走分页遍历，
    分页端点不可用时才退回老路径（并放宽上限）。
    """
    pages_err = None
    try:
        items = []
        for m in client.iter_memories(tag="kind:knowledge"):
            tags = m.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            if "kind:knowledge" in tags:
                items.append(m)
        if items:
            return items, None
    except Exception as e:  # noqa: BLE001 - 分页不可用就退回老路径
        pages_err = f"{type(e).__name__} {e}"
    try:
        return client.search_by_tag(["kind:knowledge"], match_all=True, n=100000), None
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__} {e}（分页也失败：{pages_err}）"


def _looks_generated(path) -> bool:
    """这个 `沉淀/*.md` 是我们生成的（因此可以安全删除）吗？

    认**两种**历史格式，缺一不可的判据是"没有任何手写痕迹"：

      1. 现行格式：首行 `# 沉淀：<domain>`，第二行含"由记忆层重建"
      2. 旧格式：没有标题行，第一个非空行是 `## <条目标题>`，
         且前 6 行里有 `- 类型：… ｜ … 可信度：…` 这种条目元数据行

    为什么要认第二种（2026-09-24 实测）：旧生成器 `记忆层\\distill_worker.py` 写下的
    17 个文件在去重后成了孤儿 —— 内容已并入别的 domain，文件却还在，
    人面因此显示库里没有的条目。只认新格式的话它们永远清不掉。
    `AGENTS.md` 这类手写文件（首行 `# 沉淀层…`）两种格式都不匹配，不会被删。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    if lines and lines[0].startswith("# 沉淀："):
        return any("由记忆层重建" in ln for ln in lines[:3])
    body = [ln for ln in lines[:6] if ln.strip()]
    if body and body[0].startswith("## "):
        return any(ln.startswith("- 类型：") and "可信度：" in ln for ln in lines[:6])
    return False


def rebuild_markdown(cfg, client: MemoryClient | None = None) -> dict:
    """从记忆层重建 `沉淀/*.md`，保证可读副本与库一致（索引腐化的同类问题）。"""
    import collections
    client = client or client_for(cfg)
    items, err = _knowledge_items(client)
    if err:
        return {"error": err}
    by_domain = collections.defaultdict(list)
    for m in items:
        meta = m.get("metadata") or {}
        domain = meta.get("domain") or next((t.split(":", 1)[1] for t in (m.get("tags") or [])
                                             if t.startswith("domain:")), "general")
        by_domain[domain].append((meta, m))
    sink = cfg.knowledge_dir / str(cfg.section("distill").get("sink_subdir", "沉淀"))
    sink.mkdir(parents=True, exist_ok=True)
    for domain, rows in by_domain.items():
        lines = [f"# 沉淀：{domain}", "",
                 f"> 由记忆层重建（{dt.datetime.now():%Y-%m-%d %H:%M}），共 {len(rows)} 条。", ""]
        for meta, m in rows:
            lines += [f"## {meta.get('title') or (m.get('content') or '')[:24]}", "",
                      f"- 类型：{meta.get('ktype', '-')} ｜ 复核：{meta.get('review_after', '-')}"
                      f" ｜ 来源：{meta.get('src_session', '-')}",
                      f"- 线索：{meta.get('evidence', '-')}", "",
                      (m.get("content") or "").strip(), ""]
        # 用 write_lf 而不是 Path.write_text(newline=...) —— 后者是 Python 3.10+ 才有的参数
        write_lf(sink / f"{domain}.md", "\n".join(lines) + "\n")
    # 清理"上次重建留下、这次已经没有的领域文件"：合并/改 domain 之后旧文件会变成孤儿，
    # 人面就会显示库里根本没有的条目。只删**我们自己生成的**（首行 `# 沉淀：` 且带重建标记），
    # `AGENTS.md` 这类手写文件一概不碰。
    removed = []
    for old in sorted(sink.glob("*.md")):
        if old.stem in by_domain:
            continue
        if not _looks_generated(old):
            continue
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    return {"domains": len(by_domain), "entries": len(items), "dir": str(sink),
            "removed": removed}
