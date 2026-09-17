"""分阶段检索（把 kb_lookup 的阶段预算 + recall_plus 的级联回退合并成一套）。

为什么需要级联：
  只走"语义 + 硬阈值"会 false empty —— 明明库里有记录，泛词/同义词查询却返回 0 条，
  调用方会误判成"没有历史约定"。所以从严格到宽松逐级退：
      语义@0.80 → 语义@0.72 → 语义@0.65 → FTS5 关键词 → LIKE 兜底
  并且**未命中时必须解释"为什么空"**（最高分多少、被哪一层阈值挡掉、可以怎么换招）。

为什么需要分阶段（P0–P6）：
  同一个查询意图，在不同阶段要的东西不同：P2（动手前）只要 3 条 600 字，
  P1（定方案）要 5 条 1500 字。给多了是干扰，给少了不够用。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3

from .feedback import rank_factor, reliability
from .http import MemoryAPIError, MemoryClient
from .migrate import is_procedure

JUNK_TAGS = ("kind:task", "kind:reply")   # 会话流水：噪声大，只作为最后兜底
FTS_MIN_TERM = 2


class Result:
    def __init__(self, phase, lines, used, budget, layers, diag, hashes=None):
        self.phase = phase
        self.lines = lines
        self.used = used
        self.budget = budget
        self.layers = layers
        self.diag = diag
        # 与 lines 一一对应的 content_hash（逐条归因用：谁被注入了、谁真被用上了）
        self.hashes = list(hashes or [])

    @property
    def empty(self) -> bool:
        return not self.lines

    def render(self) -> str:
        head = f"# 阶段检索 {self.phase}（{len(self.lines)} 条 / {self.used}/{self.budget} 字）"
        body = "\n".join(self.lines) if self.lines else self._why_empty()
        return head + "\n" + body

    def _why_empty(self) -> str:
        d = self.diag
        # 冷却跳过 ≠ 没命中。两者必须分清，否则调用方会以为"库里没有"而重复踩坑
        # （2026-09-16 实测：MCP 里查同一个词返回 0 条，解释却写着"库可能是空的"——是假话）
        if d.get("cooldown_skipped"):
            return ("（本次**没有真的去查**：同一阶段 + 同一查询在冷却期内，默认 10 分钟不重查）\n"
                    "· 这是「防重复」机制，不代表库里没有\n"
                    "· 要强制重查：CLI 加 `--allow-repeat`（MCP 传 `allow_repeat: true`）\n"
                    "· 换个措辞通常更好 —— 不同措辞会命中不同条目")
        lines = ["（没命中。下面说明为什么，避免把「没查到」当成「没有」）"]
        if d.get("service_down"):
            lines.append("· 记忆服务不可达 —— 不是没有记录，是查不了：先起服务")
            return "\n".join(lines)
        if d.get("candidates") == 0:
            lines.append("· 语义检索返回 0 条：库可能是空的，或嵌入模型没索引这批数据")
        else:
            lines.append(f"· 语义返回 {d['candidates']} 条，最高分 {d.get('top')}；"
                         f"达到最松阈值(0.65)的有 {d.get('above_loosest', 0)} 条")
        if d.get("fts_hits"):
            lines.append(f"· 关键词(FTS) 命中 {d['fts_hits']} 条但都被标签/预算过滤了 —— 换更具体的名词再试")
        if d.get("procedure_skipped"):
            lines.append(f"· 另有 {d['procedure_skipped']} 条**程序性内容**（技能正文）被默认跳过 ——"
                         f"它们是「照做会改变行为」的指令，只该显式加载；确实要一起看就加 "
                         f"`--include-procedure`（MCP 传 `include_procedure: true`）")
        lines.append("· 可换招：① 换措辞（同义词/术语）② 放宽 --n ③ 用 --tag domain:xxx 收窄 "
                     "④ 直接 `aml recall --grep 关键词` 翻原文")
        return "\n".join(lines)


class Retriever:
    def __init__(self, cfg, client: MemoryClient | None = None):
        self.cfg = cfg
        self.retrieval = cfg.section("retrieval")
        self.client = client or MemoryClient(cfg.api)
        self.state_file = cfg.state_dir / "lookup_state.json"
        # 程序性目录（技能正文等）：这些目录来的记录即使还没打 kind:procedure 标签，
        # 也一律按程序性内容对待 —— 存量没迁移也不会漏（见 docs/TRUST-MODEL.md）
        self.procedure_dirs = list(cfg.section("ingest").get("procedure_dirs") or [])

    # ---------------- 去重（同阶段同查询 10 分钟内不重查） ----------------
    def _load_state(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"queries": {}}

    def _save_state(self, state: dict) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def is_repeat(self, phase: str, query: str) -> bool:
        state = self._load_state()
        last = state.get("queries", {}).get(f"{phase}|{query.strip()}")
        if not last:
            return False
        try:
            when = dt.datetime.fromisoformat(last)
        except ValueError:
            return False
        return (dt.datetime.now() - when).total_seconds() < int(self.retrieval.get("cooldown_min", 10)) * 60

    def mark(self, phase: str, query: str) -> None:
        state = self._load_state()
        state.setdefault("queries", {})[f"{phase}|{query.strip()}"] = dt.datetime.now().isoformat(timespec="seconds")
        self._save_state(state)

    # ---------------- 关键词兜底（需要 db_path） ----------------
    def keyword(self, query: str, limit: int = 20):
        db = str(self.cfg.get("db_path") or "")
        if not db or not os.path.isfile(db):
            return []
        terms = [t for t in re.split(r"[\s,，、;；]+", query or "") if len(t) >= FTS_MIN_TERM]
        hits = []
        con = sqlite3.connect(db, timeout=30)
        try:
            cur = con.cursor()
            for t in terms:
                try:
                    cur.execute("SELECT m.content, m.tags, m.created_at_iso, m.content_hash, "
                                "m.metadata FROM memory_content_fts f JOIN memories m ON m.rowid = f.rowid "
                                "WHERE memory_content_fts MATCH ? LIMIT ?", (t, limit))
                    rows = cur.fetchall()
                except sqlite3.Error:
                    rows = []
                if not rows:  # FTS 对未分词的连续中文可能漏，LIKE 兜底
                    try:
                        cur.execute("SELECT content, tags, created_at_iso, content_hash, metadata "
                                    "FROM memories WHERE content LIKE ? LIMIT ?", (f"%{t}%", limit))
                        rows = cur.fetchall()
                    except sqlite3.Error:
                        rows = []
                hits += rows
        finally:
            con.close()
        out = []
        for content, tags, iso, h, meta in hits:
            out.append({"content": content, "tags": _as_list(tags), "created_at_iso": iso,
                        "content_hash": h, "metadata": _as_dict(meta)})
        return out

    # ---------------- 检索主流程 ----------------
    def search(self, query: str, phase: str = "P2", project: str | None = None,
               tag: str | None = None, n: int | None = None, allow_repeat: bool = False,
               include_procedure: bool = False) -> Result:
        phase = (phase or "P2").upper()
        spec = self.cfg.phase(phase)
        limit = int(n or spec.get("results", 5))
        budget = int(spec.get("chars", 1000))
        result_chars = int(self.retrieval.get("result_chars", 200))
        tiers = list(self.retrieval.get("cascade", [0.80, 0.72, 0.65]))
        margin = float(self.retrieval.get("rel_margin", 0.07))

        diag: dict = {"phase": phase, "candidates": 0, "top": None, "tier_used": None,
                      "above_loosest": tiers[-1] if tiers else 0.65, "fts_hits": 0,
                      "procedure_skipped": 0}
        if query and not allow_repeat and self.is_repeat(phase, query):
            diag["cooldown_skipped"] = True
            return Result(phase, [], 0, budget, [], diag)

        try:
            cand = self.client.search(query, n_results=60) if query else []
        except MemoryAPIError:
            diag["service_down"] = True
            return Result(phase, [], 0, budget, [], diag)

        # 程序性内容（技能正文等）默认不进检索：它是"照做会改变行为"的指令，
        # 只该被显式加载，不该因为语义相关就自动进上下文（见 docs/TRUST-MODEL.md）。
        # 判据两个都认：`kind:procedure` 标签（新数据）与 `kb:<程序性目录>`（历史数据）——
        # 这样存量没迁移也不漏。
        if not include_procedure:
            kept = [(s, m) for s, m in cand if not is_procedure(m, self.procedure_dirs)]
            diag["procedure_skipped"] += len(cand) - len(kept)
            cand = kept

        diag["candidates"] = len(cand)
        diag["top"] = round(cand[0][0], 3) if cand else None

        pool, tier_used = [], None
        for tier in tiers:
            hit = [(s, m) for s, m in cand if s >= tier]
            if hit:
                pool, tier_used = hit, tier
                break
        diag["tier_used"] = tier_used
        if pool:
            top = pool[0][0]
            pool = [(s, m) for s, m in pool if s >= max(tier_used, top - margin)]

        # 同档位内按"可靠度"重排：被用过且有效的排前面，没数据的**不惩罚**（系数 1.0）。
        # 只在档位内动顺序，不改分数门槛 —— 否则一条高分新记忆会因"还没被用过"被挤出结果。
        if pool:
            pool = sorted(pool, key=lambda item: -(item[0] * rank_factor(item[1].get("metadata") or {})))

        # 关键词兜底：语义没命中（或命中太少）时才用，且排在最后
        kw_pool = []
        if (not pool or len(pool) < limit) and query:
            kw_pool = [(0.0, m) for m in self.keyword(query, limit)]
            if not include_procedure:
                kept = [(s, m) for s, m in kw_pool if not is_procedure(m, self.procedure_dirs)]
                diag["procedure_skipped"] += len(kw_pool) - len(kept)
                kw_pool = kept
            diag["fts_hits"] = len(kw_pool)

        if not pool and not kw_pool:
            return Result(phase, [], 0, budget, [], diag)

        prefer = [tag] if tag else []

        def has_knowledge(m):
            return "kind:knowledge" in (m.get("tags") or [])

        def has_tag(m, t):
            return t in (m.get("tags") or [])

        picked, seen = [], set()

        def take(items):
            for s, m in items:
                h = m.get("content_hash") or (m.get("content") or "")[:80]
                if h in seen:
                    continue
                seen.add(h)
                picked.append((s, m))
                if len(picked) >= limit:
                    return True
            return False

        layers = []
        # ① 跨项目可复用知识（最高优先）
        take([(s, m) for s, m in pool if has_knowledge(m) and (not prefer or all(has_tag(m, t) for t in prefer))])
        layers.append("沉淀")
        # ② 本项目历史
        if project and len(picked) < limit:
            take([(s, m) for s, m in pool if has_tag(m, f"project:{project}")])
            layers.append("本项目")
        # ③ 全库（含关键词兜底）
        if len(picked) < limit:
            take(pool)
            take(kw_pool)
            layers.append("全库")

        lines, hashes, used = [], [], 0
        for s, m in picked:
            content = (m.get("content") or "").replace("\n", " ").strip()
            if not content:
                continue
            if used + len(content) > budget:
                content = content[: max(0, budget - used)]
            if not content:
                break
            used += len(content)
            tags = m.get("tags") or []
            meta = m.get("metadata") or {}
            domain = next((t.split(":", 1)[1] for t in tags if t.startswith("domain:")), "-")
            stamp = (m.get("created_at_iso") or "?")[:10]
            layer = "沉淀" if has_knowledge(m) else ("本项目" if project and f"project:{project}" in tags else "全库")
            stale = ""
            review_after = str(meta.get("review_after") or "").strip()
            if review_after and review_after < dt.date.today().isoformat():
                stale = f"⚠已过复核期({review_after})，先用前复核 "
            score = f"{s:.2f}" if s else "kw"
            # 只有真的有使用数据时才显示可靠度，避免给每条都挂个没意义的 1.00
            rel = reliability(meta)
            rel_text = f"|rel {rel:.2f}" if rel is not None else ""
            lines.append(f"[{layer}|{domain}|{stamp}|{score}{rel_text}] {stale}{content[:result_chars]}")
            hashes.append(m.get("content_hash") or "")
            if used >= budget:
                break

        if query and not allow_repeat:
            self.mark(phase, query)
        return Result(phase, lines, used, budget, layers, diag, hashes)

    # ---------------- 体检 ----------------
    def diagnostics(self) -> dict:
        """索引健康：总量、向量覆盖率、FTS 条数、索引.md 新鲜度。"""
        out = {"db": None, "index": None}
        db = str(self.cfg.get("db_path") or "")
        if db and os.path.isfile(db):
            con = sqlite3.connect(db, timeout=30)
            try:
                cur = con.cursor()
                total = cur.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
                try:
                    active = cur.execute(
                        "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
                except sqlite3.Error:
                    active = total
                # 向量表的真实数据在 sqlite-vec 的影子表里：
                # memory_embeddings 本身是 vec0 虚拟表，**不加载扩展就查不了**（no such module: vec0），
                # 但 rowids 影子表是普通表，能直接数。
                embedded = None
                for table in ("memory_embeddings_rowids", "memory_embeddings"):
                    try:
                        embedded = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        break
                    except sqlite3.Error:
                        continue
                try:
                    fts = cur.execute("SELECT COUNT(*) FROM memory_content_fts").fetchone()[0]
                except sqlite3.Error:
                    fts = None
                mtime = dt.datetime.fromtimestamp(os.path.getmtime(db)).strftime("%Y-%m-%d %H:%M")
                out["db"] = {"path": db, "total": total, "active": active, "embedded": embedded,
                             "fts": fts, "mtime": mtime}
            finally:
                con.close()

        index = self.cfg.knowledge_dir / "索引.md"
        if index.is_file():
            reported = None
            try:
                with open(index, encoding="utf-8") as f:
                    for line in f:
                        # 索引里的写法是 "记忆库共 **9720** 条"，中间允许 markdown 记号
                        m = re.search(r"记忆[^0-9]{0,16}([0-9][0-9,]*)\s*条", line)
                        if m:
                            reported = int(m.group(1).replace(",", ""))
                            break
            except OSError:
                pass
            out["index"] = {"path": str(index), "reported": reported,
                            "mtime": dt.datetime.fromtimestamp(os.path.getmtime(index)).strftime("%Y-%m-%d %H:%M")}
        return out


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [value]
        except ValueError:
            return [v for v in value.split(",") if v]
    return []


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}
