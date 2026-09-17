"""存量迁移测试：程序性内容的双重判据 + 迁移/回滚（假 client，不联网）。

重点：
  · `is_procedure()` 必须**两个判据都认**——新数据靠 `kind:procedure` 标签，
    历史数据靠 `kb:<程序性目录>`（存量没迁移时也不漏）
  · 迁移是"重存 + 删旧"（服务的 content_hash 把标签算进去了），必须先写快照、可回滚
  · 预览模式绝不写任何东西
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import config as cfgmod  # noqa: E402
from aml import migrate  # noqa: E402


class FakeClient:
    def __init__(self, memories):
        self.memories = list(memories)
        self.stored = []
        self.deleted = []

    def iter_memories(self, tag=None, max_pages=500):
        return list(self.memories)

    def store(self, content, tags=None, metadata=None, conversation_id=None):
        self.stored.append({"content": content, "tags": list(tags or []),
                            "metadata": metadata, "conversation_id": conversation_id})
        return {"success": True, "content_hash": f"new-{len(self.stored)}"}

    def delete(self, content_hash):
        self.deleted.append(content_hash)
        return {"success": True}


def memory(content, tags, hash_, title="标题"):
    return {"content": content, "tags": list(tags), "content_hash": hash_,
            "metadata": {"title": title}}


def make_cfg(tmp_path):
    return cfgmod.load({"aml_home": str(tmp_path)})


# ------------------------------------------------------------- 双重判据

def test_is_procedure_accepts_tag_or_source_dir(tmp_path):
    cfg = make_cfg(tmp_path)
    dirs = migrate.procedure_dirs(cfg)
    assert dirs == ["技能原始"]                       # 默认配置

    tagged = memory("正文", ["kb:技能原始", "kind:procedure"], "h1")
    legacy = memory("正文", ["kb:技能原始"], "h2")     # 存量：没标签，但来自程序性目录
    knowledge = memory("正文", ["kind:knowledge", "domain:tooling"], "h3")

    assert migrate.is_procedure(tagged, dirs) is True
    assert migrate.is_procedure(legacy, dirs) is True   # ← 这条是"存量不漏"的关键
    assert migrate.is_procedure(knowledge, dirs) is False


# ------------------------------------------------------------- 预览 / 迁移

def test_plan_counts_only_legacy_records(tmp_path):
    cfg = make_cfg(tmp_path)
    client = FakeClient([
        memory("a", ["kb:技能原始"], "h1"),
        memory("b", ["kb:技能原始", "kind:procedure"], "h2"),
        memory("c", ["kind:knowledge"], "h3"),
    ])
    info = migrate.plan(cfg, client=client)
    assert info["total"] == 2 and info["tagged"] == 1 and info["pending"] == 1
    assert info["hashes"] == ["h1"]


def test_apply_restores_tags_and_deletes_old(tmp_path):
    cfg = make_cfg(tmp_path)
    legacy = memory("技能正文", ["kb:技能原始", "knowledge-base"], "old-1")
    client = FakeClient([legacy, memory("别的知识", ["kind:knowledge"], "k1")])

    info = migrate.apply(cfg, client=client, log=lambda *_: None)
    assert info["migrated"] == 1 and info["failed"] == 0

    saved = client.stored[0]
    assert "kind:procedure" in saved["tags"] and "authority:procedure" in saved["tags"]
    assert saved["content"] == "技能正文"
    assert saved["conversation_id"].startswith("migrate:procedure:")
    assert client.deleted == ["old-1"]                 # 旧记录必须删掉（否则留无标签孤儿）

    snapshot = json.loads(open(info["snapshot"], encoding="utf-8").read())
    assert snapshot["moved"][0]["content_hash"] == "old-1"
    assert snapshot["created"] == ["new-1"]


def test_apply_skips_when_nothing_pending(tmp_path):
    cfg = make_cfg(tmp_path)
    client = FakeClient([memory("a", ["kb:技能原始", "kind:procedure"], "h1")])
    info = migrate.apply(cfg, client=client, log=lambda *_: None)
    assert info["migrated"] == 0 and info["snapshot"] is None
    assert client.stored == [] and client.deleted == []


def test_rollback_restores_originals_and_removes_tagged(tmp_path):
    cfg = make_cfg(tmp_path)
    client = FakeClient([memory("技能正文", ["kb:技能原始"], "old-1")])
    info = migrate.apply(cfg, client=client, log=lambda *_: None)

    client.stored.clear()
    client.deleted.clear()
    result = migrate.rollback(cfg, info["snapshot"], client=client, log=lambda *_: None)
    assert result["restored"] == 1 and result["removed"] == 1
    restored = client.stored[0]
    assert restored["tags"] == ["kb:技能原始"]          # 原样恢复（没被加上新标签）
    assert client.deleted == ["new-1"]                 # 删掉带标签那版


def test_limit_stops_early(tmp_path):
    cfg = make_cfg(tmp_path)
    client = FakeClient([memory(f"正文 {i}", ["kb:技能原始"], f"h{i}") for i in range(5)])
    info = migrate.plan(cfg, client=client, limit=2)
    assert info["pending"] == 2 and len(info["hashes"]) == 2
