"""蒸馏：把一段会话变成**可跨项目复用**的知识。

分工（这套分层是原系统跑了几百条沉淀之后定下来的）：
  · `kind:knowledge` + `reusable:true` + `domain:<领域>` —— 跨项目可复用，**不打 project**
  · `project:<名>` —— 只对当前项目成立的事实与流水
  · 每条知识带 `review_after`：pitfall/tooling 180 天、pattern/checklist 365 天、decision 730 天
    （技术类结论会过期，方法论相对稳定；到期由 `aml review` 提醒）

输入控制是踩过坑的：喂给模型的文本**必须截断 + 总量封顶**，
否则推理会吃光输出预算、模型返回空内容（实测出现过 1.2 万 tokens 换 0 字）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.request

from .http import MemoryClient

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
    key = os.environ.get("DISTILL_API_KEY")
    if key:
        return key
    cred = cfg.section("distill").get("credentials_file") or "~/.dsh/.credentials.yaml"
    try:
        with open(os.path.expanduser(cred), encoding="utf-8") as f:
            m = re.search(r"DEEPSEEK_API_KEY:\s*['\"]?([^'\"\s]+)", f.read())
            return m.group(1) if m else ""
    except OSError:
        return ""


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
        raise RuntimeError("没有可用的 API key：设置 DISTILL_API_KEY 或在 distill.credentials_file 指向的文件里配")
    body = json.dumps({
        "model": model or spec.get("model", "deepseek-flash"),
        "messages": [{"role": "user", "content": PROMPT + transcript}],
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
    return payload["choices"][0]["message"]["content"], payload.get("usage") or {}


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


def write_entries(cfg, entries, item, client: MemoryClient | None = None) -> int:
    """写回记忆层：kind:knowledge + domain + 复核期。"""
    client = client or MemoryClient(cfg.api)
    ok = 0
    for e in entries:
        if not isinstance(e, dict) or not e.get("body"):
            continue
        domain = (e.get("domain") or "general").strip() or "general"
        ktype = (e.get("type") or "pattern").strip() or "pattern"
        content = f"【{e.get('title', '')}】{e['body']}"
        tags = ["kind:knowledge", "reusable:true", f"domain:{domain}", f"ktype:{ktype}",
                f"confidence:{(e.get('confidence') or 'medium')}",
                f"src_session:{item.get('short', '')}"]
        metadata = {"title": e.get("title", ""), "domain": domain, "ktype": ktype,
                    "evidence": e.get("evidence", ""), "src_session": item.get("short", ""),
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
    client = client or MemoryClient(cfg.api)
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
    client = client or MemoryClient(cfg.api)
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


def rebuild_markdown(cfg, client: MemoryClient | None = None) -> dict:
    """从记忆层重建 `沉淀/*.md`，保证可读副本与库一致（索引腐化的同类问题）。"""
    import collections
    client = client or MemoryClient(cfg.api)
    try:
        items = client.search_by_tag(["kind:knowledge"], match_all=True, n=1000)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__} {e}"}
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
        (sink / f"{domain}.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return {"domains": len(by_domain), "entries": len(items), "dir": str(sink)}
