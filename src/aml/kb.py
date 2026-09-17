"""知识库侧：文档入库 + 索引重建（保证"索引不腐化"）。

两个已知坑，代码里已经按修好的写法写：
  1. 索引会腐化 —— 入库是实时的，索引若靠手工重建，必然落后（实测出现自报 8716 / 库内 9718）。
     所以索引必须能从记忆层 + 文件系统**一键重建**，并且体检里会比对两者。
  2. 文件 mtime 会被"增量入库"当成判断依据 —— 从别处拷进来的文件带着旧 mtime，
     会被增量逻辑当旧的跳过；所以入库前统一把目标文件 mtime 设成入库时刻。
"""
from __future__ import annotations

import collections
import datetime as dt
import os
import re
import shutil

from .http import MemoryClient

CHUNK = 280          # 嵌入模型有效窗口有限，块太大检索会糊
OVERLAP = 40
DEFAULT_EXTS = (".md", ".txt", ".json")


def doc_tags(cfg, group: str, path: str, mtime) -> list:
    """给一份文档算出入库标签。

    **程序性内容要单独标**：技能目录（默认 `技能原始`）里的正文是"照做会改变行为"的指令，
    不能和普通知识共享召回入口 —— 打 `kind:procedure`，检索默认会跳过它
    （见 `docs/TRUST-MODEL.md`：知道 vs 照做是两种权限）。
    """
    tags = ["knowledge-base", f"kb:{group}",
            f"file:{os.path.basename(path)[:40]}", f"date:{mtime.date().isoformat()}"]
    procedure_dirs = cfg.section("ingest").get("procedure_dirs") or []
    if group in procedure_dirs:
        tags += ["kind:procedure", "authority:procedure"]
    return tags


def chunks(content: str, size: int = CHUNK, overlap: int = OVERLAP) -> list:
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    if not content:
        return []
    if len(content) <= size:
        return [content]
    out, i = [], 0
    while i < len(content):
        out.append(content[i:i + size])
        i += size - overlap
    return out


def dir_stats(root: str):
    n = size = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".venv", "__pycache__", ".git")]
        for f in files:
            n += 1
            try:
                size += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return n, size


def fetch_memories(cfg) -> list:
    """分页取回全部记忆（索引重建用）。"""
    client = MemoryClient(cfg.api)
    page, out = 1, []
    while True:
        try:
            data = client._request(f"/api/memories?page={page}&page_size=100", None, method="GET")
        except Exception:  # noqa: BLE001
            break
        items = data.get("memories") or []
        if not items:
            break
        out += items
        if not data.get("has_more"):
            break
        page += 1
    return out


def build_index(cfg, memories=None) -> dict:
    """重建 `knowledge_dir/索引.md`（旧版留 .bak），返回统计。"""
    memories = memories if memories is not None else fetch_memories(cfg)
    kb_root = cfg.knowledge_dir
    index_path = kb_root / "索引.md"

    kb_counts = collections.Counter()
    domains = collections.defaultdict(list)
    projects = collections.Counter()
    for m in memories:
        tags = m.get("tags") or []
        for t in tags:
            if t.startswith("kb:"):
                kb_counts[t.split(":", 1)[1]] += 1
            if t.startswith("project:"):
                projects[t.split(":", 1)[1]] += 1
        if "kind:knowledge" in tags:
            meta = m.get("metadata") or {}
            dom = meta.get("domain") or next(
                (t.split(":", 1)[1] for t in tags if t.startswith("domain:")), "general")
            title = meta.get("title")
            if not title:
                content = m.get("content") or ""
                title = content[1:content.find("】")] if content.startswith("【") and "】" in content else content[:24]
            domains[dom].append(title)

    lines = []
    add = lines.append
    add("# 知识库索引")
    add("")
    add(f"> 由 `aml index rebuild` 于 {dt.datetime.now():%Y-%m-%d %H:%M} 自动生成；手工改动会被覆盖。")
    add(f"> 记忆库共 **{len(memories)}** 条（其中知识库文档 {sum(kb_counts.values())} 条）。")
    add("")
    add("## 怎么用这个索引")
    add("")
    add("| 我想要 | 去哪里 |")
    add("|---|---|")
    add("| 现成方法论 / 踩过的坑 | `沉淀/`（跨项目，按领域分文件） |")
    add("| 某主题的素材与笔记 | `源知识/` |")
    add("| 行为规则 / 输出规范 | `rules/` |")
    add("| 历史对话沉淀 | `对话记录/` |")
    add("")
    add("## 一级目录")
    add("")
    add("| 目录 | 文件 | 体积 | 检索标签 | 入库块数 |")
    add("|---|---|---|---|---|")
    if kb_root.is_dir():
        for name in sorted(os.listdir(kb_root)):
            path = os.path.join(kb_root, name)
            if not os.path.isdir(path):
                continue
            n, size = dir_stats(path)
            add(f"| `{name}/` | {n} | {size/1e6:.2f} MB | kb:{name} | {kb_counts.get(name, 0) or '-'} |")
        for name in sorted(os.listdir(kb_root)):
            path = os.path.join(kb_root, name)
            if os.path.isfile(path) and name.endswith(".md") and name != "索引.md":
                add(f"| `{name}` | 1 | {os.path.getsize(path)/1e3:.1f} KB | kb:根目录 | - |")
    add("")
    add(f"## 沉淀层（跨项目可复用知识，{sum(len(v) for v in domains.values())} 条）")
    add("")
    add("写回规则：可跨项目复用的经验打 `kind:knowledge` + `reusable:true` + `domain:*`，**不打 project**；")
    add("项目专属事实才打 `project:<名>`。检索时先看这一层。")
    add("")
    for dom in sorted(domains, key=lambda d: -len(domains[d])):
        add(f"### {dom}（{len(domains[dom])} 条）")
        add("")
        add(f"文件：`沉淀/{dom}.md`")
        add("")
        for title in domains[dom]:
            add(f"- {title}")
        add("")
    add("## 项目分布")
    add("")
    add("| 项目 | 条数 |")
    add("|---|---|")
    for name, count in projects.most_common(15):
        add(f"| `{name}` | {count} |")
    add("")
    add("## 自动维护")
    add("")
    add("- 会话入库：`aml sync`（或常驻 `aml watch`）")
    add("- 蒸馏成跨项目知识：`aml distill`")
    add("- 索引重建：`aml index rebuild`（大批入库后跑一次；`aml doctor` 会告警过期）")

    if index_path.exists():
        shutil.copy2(index_path, str(index_path) + ".bak")
    kb_root.mkdir(parents=True, exist_ok=True)
    with open(index_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return {"path": str(index_path), "memories": len(memories),
            "knowledge": sum(len(v) for v in domains.values()), "domains": len(domains),
            "dirs": len([d for d in os.listdir(kb_root) if os.path.isdir(os.path.join(kb_root, d))])}


def ingest_docs(cfg, dirs=None, exts=None, since: str | None = None, dry_run: bool = False,
                progress=None) -> dict:
    """把知识库目录里的文档分块灌进记忆层（走 HTTP，服务端按内容去重）。

    dirs 缺省 = knowledge_dir 下的全部一级子目录 + 根目录 md。
    since 传 ISO 时间时只灌 mtime 更新的文件（增量；配合镜像时 os.utime 使用）。
    """
    kb_root = cfg.knowledge_dir
    exts = tuple(exts or DEFAULT_EXTS)
    since_ts = None
    if since:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                since_ts = dt.datetime.strptime(since, fmt).timestamp()
                break
            except ValueError:
                continue

    files = []
    targets = dirs if dirs is not None else [d for d in sorted(os.listdir(kb_root))
                                             if os.path.isdir(os.path.join(kb_root, d))] if kb_root.is_dir() else []
    for name in targets:
        root = os.path.join(kb_root, name)
        if not os.path.isdir(root):
            continue
        for r, sub, fs in os.walk(root):
            sub[:] = [d for d in sub if d not in (".git", "__pycache__", ".venv")]
            for f in fs:
                path = os.path.join(r, f)
                if os.path.splitext(f)[1].lower() not in exts:
                    continue
                if since_ts:
                    try:
                        if os.path.getmtime(path) < since_ts:
                            continue
                    except OSError:
                        pass
                files.append((name, path))
    if dirs is None and kb_root.is_dir():
        for f in os.listdir(kb_root):
            path = os.path.join(kb_root, f)
            if os.path.isfile(path) and os.path.splitext(f)[1].lower() in exts and f != "索引.md":
                files.append(("根目录", path))

    plan = []
    total_chunks = 0
    for name, path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        parts = chunks(content)
        if parts:
            plan.append((name, path, parts))
            total_chunks += len(parts)
    if dry_run:
        return {"files": len(plan), "chunks": total_chunks, "dry_run": True}

    client = MemoryClient(cfg.api)
    ok = dup = err = done = 0
    for name, path, parts in plan:
        try:
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(path), dt.timezone.utc)
        except OSError:
            mtime = dt.datetime.now(dt.timezone.utc)
        stamp = mtime.isoformat().replace("+00:00", "Z")
        tags = doc_tags(cfg, name, path, mtime)
        for i, part in enumerate(parts):
            payload_meta = {"timestamp": stamp, "path": path, "source_agent": "knowledge-base",
                            "chunk": i, "chunks": len(parts), "file": os.path.basename(path)}
            try:
                res = client.store(part, tags, payload_meta, f"kb:{path}")
                ok += 1 if res.get("success") else 0
                dup += 0 if res.get("success") else 1
            except Exception:  # noqa: BLE001
                err += 1
            done += 1
            if progress and done % 100 == 0:
                progress(done, total_chunks, ok, dup, err)
    return {"files": len(plan), "chunks": total_chunks, "ok": ok, "dup": dup, "error": err}
