"""知识去重合并：同一经验被多个会话反复蒸馏，会在记忆层里堆成近义条目。

现象：问一个泛词，命中 8 条近义知识，互相挤占 P1 的 5 条预算。

做法（照搬原系统跑了几百条沉淀后的口径）：
  1. 用**字面相似度聚簇**：正文二元组 Jaccard ≥ `sim`（默认 0.16）或标题 ≥ `title_sim`（0.40），
     并集查并；
  2. 领域约束：**不同领域的条目只在"几乎逐字重复"（≥0.30）时才合并** ——
     否则会把"相关但不同"的两条揉成一条，损失信息；
  3. 每簇交给 LLM 合并成 1 条（保留各自独有信息点；**矛盾时以时间较新的为准**，
     并在正文写明"早期结论已作废"）；
  4. 写入合并条目 → 删除原条目，**删前整簇备份**（可 `--rollback` 回滚）；
  5. 已合并的原条目在 metadata 里留 `merged_from`，便于溯源。

默认只预览（dry-run）：这一操作会真删记录，必须先看清要合并什么。
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import re

from .distill import call_llm, review_after_for
from .http import MemoryClient

MERGE_PROMPT = """下面是同一主题的若干条已入库知识（来自不同会话，内容高度重叠）。
请合并成 **1 条**：
- 保留各自独有的信息点，去掉重复表述，不要丢事实；
- **若条目之间存在矛盾或过时结论，以时间较新的为准**，并在正文里写明「早期结论已作废：…」，
  不要两个版本都留着让人猜哪个对；
- 正文自包含、脱离原项目也能看懂，≤400 字；
- 只输出一个 JSON 对象（不是数组），字段：
{"title":"≤20字","domain":"kebab-case领域","type":"pitfall|pattern|decision|tooling|checklist",
 "body":"合并后的正文","evidence":"合并后的来源线索","confidence":"high|medium|low"}
只输出 JSON，不要解释、不要代码块标记。

待合并的知识（越靠后入库时间越新）：
"""


# ------------------------------------------------------------------ 聚簇

def grams(text: str) -> set:
    """二元组集合（去掉空白）。中文没词边界，二元组比按词切更稳。"""
    s = "".join(ch for ch in (text or "") if not ch.isspace())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster(entries: list, sim: float = 0.16, title_sim: float = 0.40,
            domain_guard: float = 0.30) -> list:
    """返回重复簇（每簇是下标列表，长度 ≥2）。"""
    n = len(entries)
    body = [grams(e.get("content")) for e in entries]
    title = [grams((e.get("metadata") or {}).get("title")) for e in entries]
    domain = [((e.get("metadata") or {}).get("domain") or "").strip() for e in entries]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            body_sim, title_j = jaccard(body[i], body[j]), jaccard(title[i], title[j])
            if body_sim < sim and title_j < title_sim:
                continue
            if domain[i] and domain[j] and domain[i] != domain[j] and body_sim < domain_guard:
                continue
            union(i, j)

    groups = collections.defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return [sorted(v) for v in groups.values() if len(v) > 1]


def knowledge_entries(cfg, client: MemoryClient | None = None) -> list:
    """取所有带标题的知识条目（只有这些才参与合并）。"""
    client = client or MemoryClient(cfg.api)
    out = []
    for memory in client.iter_memories(tag="kind:knowledge"):
        if (memory.get("metadata") or {}).get("title"):
            out.append(memory)
    return out


def plan(cfg, sim: float = 0.16, title_sim: float = 0.40, client: MemoryClient | None = None,
         limit: int = 0) -> dict:
    """预览：返回簇、可减少条数。不写任何东西。"""
    entries = knowledge_entries(cfg, client)
    if limit:
        entries = entries[:limit]
    groups = cluster(entries, sim=sim, title_sim=title_sim)
    groups.sort(key=len, reverse=True)
    return {"entries": len(entries), "clusters": groups,
            "reducible": sum(len(g) - 1 for g in groups)}


def cluster_titles(entries: list, groups: list) -> list:
    return [[(e.get("metadata") or {}).get("title") or (e.get("content") or "")[:24]
             for e in (entries[i] for i in g)] for g in groups]


# ------------------------------------------------------------------ 合并

def merge_cluster(cluster_mems: list, cfg, call=None) -> dict:
    """把一簇交给 LLM 合并成 1 条（返回解析后的对象）。"""
    parts = []
    for memory in sorted(cluster_mems, key=lambda x: float(x.get("created_at") or 0)):
        meta = memory.get("metadata") or {}
        day = ((memory.get("created_at_iso") or "") or "?")[:10]
        parts.append(f"- [{day}｜{meta.get('ktype')}/{meta.get('domain')}] "
                     f"{meta.get('title')}：{(memory.get('content') or '')}")
    text, usage = (call or call_llm)(cfg, MERGE_PROMPT + "\n".join(parts))
    cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没返回 JSON 对象")
    merged = json.loads(cleaned[start:end + 1])
    merged["_usage"] = usage
    return merged


def apply_cluster(cfg, cluster_mems: list, merged: dict, client: MemoryClient | None = None) -> dict:
    """写入合并条目并删除原条目。"""
    client = client or MemoryClient(cfg.api)
    domain = (merged.get("domain") or "general").strip() or "general"
    ktype = (merged.get("type") or "pattern").strip() or "pattern"
    sessions = sorted({(m.get("metadata") or {}).get("src_session") or "-" for m in cluster_mems})
    titles = [(m.get("metadata") or {}).get("title") for m in cluster_mems]
    content = f"【{merged.get('title', '')}】{merged.get('body', '')}".strip()
    metadata = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_agent": "aml-dedup",
        "src_session": sessions[0] if sessions else "-",
        "src_sessions": sessions,
        "evidence": (merged.get("evidence") or "")[:300],
        "title": merged.get("title"), "domain": domain, "ktype": ktype,
        "confidence": merged.get("confidence"),
        "merged_from": [{"hash": m.get("content_hash"),
                         "title": (m.get("metadata") or {}).get("title")} for m in cluster_mems],
        "merged_count": len(cluster_mems),
        "review_after": review_after_for(ktype),
    }
    tags = ["kind:knowledge", "reusable:true", f"domain:{domain}", f"ktype:{ktype}",
            f"confidence:{merged.get('confidence', 'medium')}",
            f"src_session:{sessions[0] if sessions else '-'}"]
    response = client.store(content[:1500], tags, metadata,
                            conversation_id=f"knowledge:merged:{domain}")
    if not response.get("success"):
        raise RuntimeError(f"写入合并条目失败：{str(response)[:120]}")
    # 合并条目的 hash 要从写入响应里拿：rollback 时靠它删掉"合并出来的那条"。
    # 不同版本的响应字段名不一样，所以多试几个；拿不到就让调用方知道（回滚需手动删）。
    merged_hash = (response.get("content_hash") or response.get("hash") or response.get("id")
                   or (response.get("memory") or {}).get("content_hash"))
    deleted = []
    for memory in cluster_mems:
        content_hash = memory.get("content_hash")
        if not content_hash:
            continue
        try:
            client.delete(content_hash)
            deleted.append(content_hash)
        except Exception:  # noqa: BLE001 - 删不掉不算致命，快照里有原文
            pass
    return {"titles": titles, "merged_title": merged.get("title"), "domain": domain,
            "sources": sessions, "deleted": deleted, "merged_hash": merged_hash,
            "tokens": (merged.get("_usage") or {}).get("total_tokens")}


# -------------------------------------------------------------- 快照 / 回滚

def snapshot_path(cfg) -> object:
    directory = cfg.backups_dir / "dedup"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"dedup-{dt.datetime.now():%Y%m%d-%H%M%S}.json"


def save_snapshot(cfg, snapshot: dict):
    path = snapshot_path(cfg)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)
    log = cfg.state_dir / "dedup_log.json"
    history = []
    if log.is_file():
        try:
            with open(log, encoding="utf-8") as f:
                history = json.load(f)
        except (OSError, ValueError):
            history = []
    history.append({"at": snapshot.get("at"), "clusters": len(snapshot.get("results") or []),
                    "backup": str(path)})
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8", newline="\n") as f:
        json.dump(history[-50:], f, ensure_ascii=False, indent=1)
    return path


def rollback(cfg, path: str, client: MemoryClient | None = None) -> dict:
    """回滚一次合并：恢复原条目、删除合并出来的条目。"""
    client = client or MemoryClient(cfg.api)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    restored = 0
    for memory in data.get("memories") or []:
        response = client.store(memory.get("content"), memory.get("tags") or [],
                                memory.get("metadata") or {},
                                conversation_id=f"rollback:{memory.get('content_hash')}")
        restored += 1 if response.get("success") else 0
    removed = 0
    for item in data.get("results") or []:
        for content_hash in [item.get("merged_hash")] if item.get("merged_hash") else []:
            try:
                client.delete(content_hash)
                removed += 1
            except Exception:  # noqa: BLE001
                pass
    return {"restored": restored, "removed": removed, "from": path,
            "manual_cleanup": [item.get("merged_title") for item in (data.get("results") or [])
                               if not item.get("merged_hash")]}


# ------------------------------------------------------------------ 主流程

def run(cfg, apply: bool = False, sim: float = 0.16, title_sim: float = 0.40, show: int = 12,
        limit: int = 0, client: MemoryClient | None = None, call=None, log=print) -> dict:
    """预览或执行合并。apply=False 时绝不写任何东西。"""
    client = client or MemoryClient(cfg.api)
    preview = plan(cfg, sim=sim, title_sim=title_sim, client=client, limit=limit)
    entries = knowledge_entries(cfg, client)[:limit or None]
    groups = preview["clusters"]
    result = {"entries": preview["entries"], "clusters": len(groups),
              "reducible": preview["reducible"], "merged": 0, "failed": 0, "backup": None}
    log(f"知识条目 {preview['entries']} 条 → {len(groups)} 个重复簇，合并后可减少 {preview['reducible']} 条"
        f"（正文≥{sim} 或 标题≥{title_sim}）")
    for group in groups[:show]:
        log(f"--- 簇（{len(group)} 条）---")
        for index in group:
            log("    · " + str((entries[index].get("metadata") or {}).get("title")))
    if len(groups) > show:
        log(f"... 还有 {len(groups) - show} 个簇")

    if not apply:
        result["dry_run"] = True
        log("（预览模式，未改动任何数据；确认后加 --apply）")
        return result
    if not groups:
        return result

    snapshot = {"at": dt.datetime.now().isoformat(timespec="seconds"),
                "params": {"sim": sim, "title_sim": title_sim},
                "memories": [], "results": []}
    for index, group in enumerate(groups, 1):
        memories = [entries[i] for i in group]
        try:
            merged = merge_cluster(memories, cfg, call=call)
            outcome = apply_cluster(cfg, memories, merged, client)
            snapshot["memories"] += [{"content": m.get("content"), "tags": m.get("tags"),
                                      "metadata": m.get("metadata"),
                                      "content_hash": m.get("content_hash")} for m in memories]
            snapshot["results"].append(outcome)
            result["merged"] += 1
            log(f"[{index}/{len(groups)}] 合并 {len(memories)} 条 → 「{outcome['merged_title']}」"
                f"（{outcome.get('tokens', '?')} tokens）")
            if not outcome.get("merged_hash"):
                log("    ⚠ 没拿到合并条目的 hash：回滚时这一条需要你手动删（其余会自动恢复）")
        except Exception as e:  # noqa: BLE001
            result["failed"] += 1
            log(f"[{index}/{len(groups)}] 失败：{type(e).__name__} {str(e)[:100]}")
    if snapshot["results"]:
        result["backup"] = str(save_snapshot(cfg, snapshot))
        log(f"快照已存：{result['backup']}（回滚：aml dedup --rollback <该文件>）")
        log("建议接着重建可读副本：aml distill --rebuild-md")
    return result
