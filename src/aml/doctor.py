"""体检：一条命令回答"这套记忆层现在健康吗、哪里不对、怎么修"。

检查项刻意都是**可自动修复**或有明确处置动作的，避免输出一堆没用的指标。
"""
from __future__ import annotations

from .adapters import build
from .http import MemoryAPIError, MemoryClient
from .retrieval import Retriever

OK, WARN, BAD = "ok", "warn", "bad"


class Check:
    def __init__(self, name, status, detail="", fix=""):
        self.name, self.status, self.detail, self.fix = name, status, detail, fix

    def line(self) -> str:
        icon = {OK: "✅", WARN: "⚠️", BAD: "❌"}[self.status]
        out = f"{icon} {self.name}：{self.detail}"
        if self.fix and self.status != OK:
            out += f"\n     修复：{self.fix}"
        return out


def run(cfg) -> list:
    checks = []

    # 1. 目录
    for label, path in (("数据主目录", cfg.home), ("知识库目录", cfg.knowledge_dir),
                        ("状态目录", cfg.state_dir), ("备份目录", cfg.backups_dir)):
        if path.is_dir():
            checks.append(Check(label, OK, str(path)))
        else:
            checks.append(Check(label, WARN, f"{path} 不存在",
                                "aml init（会创建目录骨架与 config.yaml）"))

    # 2. 服务
    client = MemoryClient(cfg.api)
    try:
        client.health()
        checks.append(Check("记忆服务", OK, f"{cfg.api} 健康"))
    except MemoryAPIError as e:
        checks.append(Check("记忆服务", BAD, str(e)[:120],
                            "先启动 mcp-memory-service，再重跑 aml doctor"))

    # 3. 数据库 / 索引新鲜度
    retriever = Retriever(cfg, client)
    diag = retriever.diagnostics()
    db = diag.get("db")
    if db:
        embedded = db.get("embedded")
        if embedded is None:
            checks.append(Check("向量覆盖", WARN, "查不到向量表（可能需要 sqlite-vec 扩展）",
                                "缺失向量的记录永远搜不到：aml maintenance backfill-embeddings"))
        else:
            coverage = (embedded / db["total"] * 100) if db["total"] else 0
            status = OK if coverage >= 99.5 else (WARN if coverage >= 90 else BAD)
            checks.append(Check("向量覆盖", status, f"{embedded}/{db['total']}（{coverage:.1f}%）",
                                "aml maintenance backfill-embeddings（缺失的记录永远搜不到）"))
        checks.append(Check("记忆条数", OK, f"库内 {db['total']}（有效 {db.get('active', db['total'])}），"
                                            f"库更新于 {db['mtime']}"))
        if db.get("fts"):
            checks.append(Check("FTS 全文索引", OK, f"{db['fts']} 条"))
        else:
            checks.append(Check("FTS 全文索引", WARN, "没有 FTS 数据",
                                "关键词兜底会失效，建议启用 memory_content_fts"))
    else:
        checks.append(Check("数据库", WARN, "未配置 db_path，跳过直连检查",
                            "在 config.yaml 里设置 db_path 指向记忆服务的 sqlite_vec.db"))

    index = diag.get("index")
    if index:
        if db and index.get("reported") is not None:
            gap = db["total"] - index["reported"]
            if gap <= max(20, db["total"] * 0.01):
                checks.append(Check("索引新鲜度", OK, f"索引自报 {index['reported']} ≈ 库内 {db['total']}"))
            else:
                checks.append(Check("索引新鲜度", WARN,
                                    f"索引自报 {index['reported']}，库内 {db['total']}，差 {gap} 条",
                                    "python -m aml index rebuild"))
        else:
            checks.append(Check("索引新鲜度", OK, f"索引存在（{index['mtime']}）"))
    else:
        checks.append(Check("知识库索引", WARN, f"{cfg.knowledge_dir / '索引.md'} 不存在",
                            "python -m aml index rebuild"))

    # 4. 适配器能不能看到会话
    for adapter in build(cfg):
        try:
            files = adapter.discover()
            status = OK if files else WARN
            detail = f"{len(files)} 个会话文件"
            fix = "" if files else "确认该 agent 装过/用过；或改 config.yaml 里这个 adapter 的路径"
        except Exception as e:  # noqa: BLE001
            status, detail, fix = WARN, f"扫描失败：{type(e).__name__} {e}", "检查路径配置"
        checks.append(Check(f"适配器 {adapter.name}", status, detail, fix))

    # 5. 阈值自测：拿一个高频词试一下，确认"能查得到"
    if db and db.get("total"):
        result = retriever.search("知识库", phase="P2", n=1, allow_repeat=True)
        if result.empty and diag.get("db"):
            checks.append(Check("检索自测", WARN, "一个泛词查询 0 命中",
                                "可能是阈值过严或嵌入模型不匹配；用 aml search --explain 看诊断"))
        else:
            checks.append(Check("检索自测", OK, f"泛词查询命中 {len(result.lines)} 条"
                                                f"（tier {result.diag.get('tier_used')}）"))
    return checks


def render(checks) -> str:
    bad = sum(1 for c in checks if c.status == BAD)
    warn = sum(1 for c in checks if c.status == WARN)
    head = f"记忆层体检：{len(checks)} 项，❌ {bad}，⚠️ {warn}"
    return head + "\n" + "\n".join(c.line() for c in checks)
