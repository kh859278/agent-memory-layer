"""去重合并与向量补齐的测试（全部离线：假 client、临时 sqlite）。

这两块都碰"真删数据"，所以测试的重点不是"功能跑通"，而是：
  · 预览模式**绝不写任何东西**
  · 合并前有整簇快照、能回滚
  · 孤儿清理只删"有同内容带向量副本"的那些
"""
from __future__ import annotations

import json
import sqlite3
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import config as cfgmod  # noqa: E402
from aml import dedup, embeddings  # noqa: E402


def entry(title, content, domain="env-windows", created=1700000000, hash_=None, ktype="pitfall"):
    return {"content": content, "tags": ["kind:knowledge", f"domain:{domain}"],
            "metadata": {"title": title, "domain": domain, "ktype": ktype},
            "created_at": created, "created_at_iso": "2026-09-13T00:00:00Z",
            "content_hash": hash_ or f"h-{title}"}


# --------------------------------------------------------------- 聚簇逻辑

def test_grams_and_jaccard():
    assert dedup.jaccard(dedup.grams("abc"), dedup.grams("abc")) == 1.0
    assert dedup.jaccard(dedup.grams("abc"), dedup.grams("xyz")) == 0.0
    assert dedup.jaccard(set(), dedup.grams("abc")) == 0.0
    # 空白不影响
    assert dedup.jaccard(dedup.grams("a b c"), dedup.grams("abc")) == 1.0


def test_cluster_groups_near_duplicates_only():
    entries = [
        entry("PowerShell 乱码", "Windows PowerShell 5.1 默认按 GBK 解析脚本，UTF-8 保存的中文会乱码，改用 pwsh7 或带 BOM。"),
        entry("PowerShell 编码", "Windows PowerShell 5.1 默认按 GBK 解析脚本文件，UTF-8 保存的中文会乱码，解决办法是改用 pwsh7 或存成带 BOM。"),
        entry("抓取限流", "限流要按小时级冷却，33 到 58 分钟都不够，必须断点续传并按质量校验。"),
    ]
    groups = dedup.cluster(entries)
    assert len(groups) == 1
    assert sorted(groups[0]) == [0, 1]


def test_cluster_respects_domain_guard():
    """不同领域、只是"有点像"的两条不该被合并（跨领域合并要求近乎逐字重复）。"""
    a = entry("PowerShell 脚本乱码",
              "PowerShell 5.1 按 GBK 解析脚本，UTF-8 存的中文脚本会乱码，改用 pwsh 7 或存成带 BOM。",
              domain="env-windows")
    b = entry("抓取限流冷却",
              "抓取要按小时级冷却，33 到 58 分钟都不够，必须断点续传并按质量校验回源。",
              domain="data-scraping")
    assert dedup.cluster([a, b]) == []          # 领域不同 + 正文相似度 <0.30 → 不并

    # 同一领域、近乎重复 → 合并
    c = entry("PowerShell 乱码", "PowerShell 5.1 按 GBK 解析脚本，UTF-8 存的中文脚本会乱码，"
                                "改用 pwsh 7 或存成带 BOM 更稳。", domain="env-windows")
    assert len(dedup.cluster([a, c])) == 1


# ------------------------------------------------------- 预览 / 合并 / 回滚

class FakeClient:
    """记录所有写操作；iter_memories 返回注入的知识条目。"""

    def __init__(self, entries=None):
        self.entries = entries or []
        self.stored = []
        self.deleted = []

    def iter_memories(self, tag=None, max_pages=500):
        return list(self.entries)

    def store(self, content, tags=None, metadata=None, conversation_id=None):
        self.stored.append({"content": content, "tags": tags, "metadata": metadata,
                            "conversation_id": conversation_id})
        # 真实服务会在响应里回 content_hash —— rollback 就靠它删掉"合并出来的那条"
        return {"success": True, "content_hash": f"merged-{len(self.stored)}"}

    def delete(self, content_hash):
        self.deleted.append(content_hash)
        return {"success": True}


def fake_call(cfg, transcript, model=None):
    assert "待合并的知识" in transcript
    return json.dumps({"title": "合并后的标题", "domain": "env-windows", "type": "pitfall",
                       "body": "合并后的正文（保留了各自独有信息点）。",
                       "evidence": "两个会话", "confidence": "high"}), {"total_tokens": 123}


def test_preview_never_writes(tmp_path):
    client = FakeClient([
        entry("A", "同一段经验的两种说法，内容几乎一致，只是措辞不同而已。"),
        entry("B", "同一段经验的两种说法，内容几乎一致，只是措辞略有不同而已。"),
    ])
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    result = dedup.run(cfg, apply=False, client=client, call=fake_call, log=lambda *_: None)
    assert result["clusters"] == 1 and result["dry_run"] is True
    assert client.stored == [] and client.deleted == []


def test_apply_merges_and_writes_backup_that_can_rollback(tmp_path):
    client = FakeClient([
        entry("A", "同一段经验的两种说法，内容几乎一致，只是措辞不同而已。", hash_="h1"),
        entry("B", "同一段经验的两种说法，内容几乎一致，只是措辞略有不同而已。", hash_="h2"),
    ])
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    result = dedup.run(cfg, apply=True, client=client, call=fake_call, log=lambda *_: None)
    assert result["merged"] == 1 and result["failed"] == 0
    assert client.stored[0]["conversation_id"].startswith("knowledge:merged:")
    assert client.stored[0]["metadata"]["merged_count"] == 2
    assert sorted(client.deleted) == ["h1", "h2"]

    backup = result["backup"]
    assert backup and backup.endswith(".json")
    data = json.loads(open(backup, encoding="utf-8").read())
    assert len(data["memories"]) == 2                     # 整簇都备份了

    # 回滚：原条目重新写入，合并条目被删掉
    client.stored.clear()
    client.deleted.clear()
    info = dedup.rollback(cfg, backup, client=client)
    assert info["restored"] == 2
    assert client.deleted == ["merged-1"]                  # 只删那条合并出来的
    assert info["manual_cleanup"] == []


def test_rollback_reports_when_merged_hash_is_unknown(tmp_path):
    """服务响应里没有 content_hash 时，回滚要明确说"这条得你手动删"，而不是静默漏掉。"""
    class NoHashClient(FakeClient):
        def store(self, content, tags=None, metadata=None, conversation_id=None):
            self.stored.append({"content": content, "tags": tags, "metadata": metadata})
            return {"success": True}

    client = NoHashClient([
        entry("A", "同一段经验的两种说法，内容几乎一致，只是措辞不同而已。", hash_="h1"),
        entry("B", "同一段经验的两种说法，内容几乎一致，只是措辞略有不同而已。", hash_="h2"),
    ])
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    result = dedup.run(cfg, apply=True, client=client, call=fake_call, log=lambda *_: None)
    assert result["merged"] == 1
    client.deleted.clear()
    info = dedup.rollback(cfg, result["backup"], client=client)
    assert info["restored"] == 2 and info["removed"] == 0
    assert info["manual_cleanup"] == ["合并后的标题"]


def test_merge_cluster_rejects_non_json(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    try:
        dedup.merge_cluster([entry("A", "内容")], cfg, call=lambda *a, **k: ("模型胡说八道", {}))
    except ValueError as e:
        assert "JSON" in str(e)
    else:  # pragma: no cover
        raise AssertionError("模型没返回 JSON 时应当报错")


# --------------------------------------------------------- 向量补齐（sqlite）

def make_db(path, rows):
    """rows: [(rowid, content_hash, content, has_vector)]"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memories (rowid INTEGER PRIMARY KEY, content_hash TEXT, content TEXT, "
                "tags TEXT, metadata TEXT, created_at REAL)")
    con.execute("CREATE TABLE memory_embeddings_rowids (rowid INTEGER PRIMARY KEY)")
    for rowid, content_hash, content, has_vector in rows:
        con.execute("INSERT INTO memories VALUES (?,?,?,?,?,?)",
                    (rowid, content_hash, content, json.dumps(["kind:knowledge"]),
                     json.dumps({"title": content[:10]}), 1700000000 + rowid))
        if has_vector:
            con.execute("INSERT INTO memory_embeddings_rowids VALUES (?)", (rowid,))
    con.commit()
    con.close()
    return path


def test_stats_counts_missing_vectors(tmp_path):
    db = make_db(tmp_path / "vec.db", [(1, "h1", "有向量的记录", True),
                                       (2, "h2", "缺向量的记录", False),
                                       (3, "h3", "也缺向量", False)])
    cfg = cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})
    info = embeddings.stats(cfg)
    assert info["total"] == 3 and info["missing"] == 2
    assert info["sample"][0][0] == 2


def test_stats_without_db_path_explains_how_to_fix(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    assert "db_path" in embeddings.stats(cfg)["error"]


def test_backfill_dry_run_then_apply_uses_conversation_id(tmp_path):
    db = make_db(tmp_path / "vec.db", [(1, "h1", "有向量", True), (2, "h2", "缺向量", False)])
    cfg = cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})
    client = FakeClient()

    preview = embeddings.backfill(cfg, apply=False, client=client, log=lambda *_: None)
    assert preview["dry_run"] is True and client.stored == []

    applied = embeddings.backfill(cfg, apply=True, client=client, log=lambda *_: None)
    assert applied["ok"] == 1 and applied["failed"] == 0
    assert client.stored[0]["conversation_id"] == "embed-backfill"
    assert client.stored[0]["content"] == "缺向量"


def test_prune_orphans_only_deletes_duplicates(tmp_path):
    # 2 与 1 内容相同且 1 有向量 → 2 是孤儿；3 缺向量但没有副本 → 不该删
    db = make_db(tmp_path / "vec.db", [(1, "h1", "重复内容", True),
                                       (2, "h2", "重复内容", False),
                                       (3, "h3", "独有内容", False)])
    cfg = cfgmod.load({"aml_home": str(tmp_path), "db_path": str(db)})
    preview = embeddings.prune_orphans(cfg, apply=False)
    assert preview["orphans"] == 1 and preview["deleted"] == 0

    info = embeddings.prune_orphans(cfg, apply=True)
    assert info["deleted"] == 1
    con = sqlite3.connect(str(db))
    left = [row[0] for row in con.execute("SELECT rowid FROM memories ORDER BY rowid")]
    con.close()
    assert left == [1, 3]
