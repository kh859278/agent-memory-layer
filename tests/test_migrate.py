"""存量迁移测试：程序性内容的双重判据 + 原地改标签 + 回滚（假 client，不联网）。

关键教训（写进测试，防止改回去）：
  · 迁移**不能**用"重存 + 删旧"：服务按逐字内容判重 → `Duplicate content detected`，
    带 conversation_id 也只绕过语义去重（本机 2804 条全失败）
  · 正确做法是 `PUT /api/memories/{hash}` 原地改 tags：不重嵌入、hash 不变、可回滚
  · `is_procedure()` 必须**两个判据都认**（标签 + 来源目录），否则存量没迁移就漏
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import config as cfgmod  # noqa: E402
from aml import migrate  # noqa: E402
from aml.http import MemoryClient  # noqa: E402


class FakeClient:
    def __init__(self, memories):
        self.memories = list(memories)
        self.updated = []
        self.stored = []
        self.deleted = []

    def iter_memories(self, tag=None, max_pages=500):
        return list(self.memories)

    def update(self, content_hash, updates):
        self.updated.append((content_hash, updates))
        return {"success": True}

    # 下面两个**不该被调用**（迁移只走原地更新），留着是为了让"用错方法"直接暴露
    def store(self, *a, **kw):
        self.stored.append((a, kw))
        return {"success": True}

    def delete(self, *a, **kw):
        self.deleted.append(a)
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
    assert migrate.is_procedure(legacy, dirs) is True   # ← "存量不漏"的关键
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
    assert [r["content_hash"] for r in info["records"]] == ["h1"]
    assert info["records"][0]["tags"] == ["kb:技能原始"]


def test_apply_updates_tags_in_place(tmp_path):
    cfg = make_cfg(tmp_path)
    client = FakeClient([
        memory("技能正文", ["kb:技能原始", "knowledge-base"], "h1"),
        memory("别的知识", ["kind:knowledge"], "k1"),
    ])

    info = migrate.apply(cfg, client=client, log=lambda *_: None)
    assert info["migrated"] == 1 and info["failed"] == 0

    hashes = [h for h, _ in client.updated]
    assert hashes == ["h1"]                            # 只动该动的
    tags = client.updated[0][1]["tags"]
    assert "kind:procedure" in tags and "authority:procedure" in tags
    assert "kb:技能原始" in tags                        # 原有标签不丢
    assert client.stored == [] and client.deleted == []  # 绝不用重存/删旧

    snapshot = json.loads(open(info["snapshot"], encoding="utf-8").read())
    assert snapshot["moved"][0] == {"content_hash": "h1", "tags": ["kb:技能原始", "knowledge-base"]}


def test_apply_skips_when_nothing_pending(tmp_path):
    cfg = make_cfg(tmp_path)
    client = FakeClient([memory("a", ["kb:技能原始", "kind:procedure"], "h1")])
    info = migrate.apply(cfg, client=client, log=lambda *_: None)
    assert info["migrated"] == 0 and info["snapshot"] is None
    assert client.updated == []


def test_rollback_writes_original_tags_back(tmp_path):
    cfg = make_cfg(tmp_path)
    client = FakeClient([memory("技能正文", ["kb:技能原始"], "h1")])
    info = migrate.apply(cfg, client=client, log=lambda *_: None)

    client.updated.clear()
    result = migrate.rollback(cfg, info["snapshot"], client=client, log=lambda *_: None)
    assert result["restored"] == 1
    assert client.updated[0] == ("h1", {"tags": ["kb:技能原始"]})   # 原样写回，没有新标签


def test_limit_stops_early(tmp_path):
    cfg = make_cfg(tmp_path)
    client = FakeClient([memory(f"正文 {i}", ["kb:技能原始"], f"h{i}") for i in range(5)])
    info = migrate.plan(cfg, client=client, limit=2)
    assert info["pending"] == 2 and len(info["records"]) == 2


# ------------------------------------------------- 客户端方法本身（端点是踩过的坑）

def test_client_update_uses_put_and_drops_unknown_fields(monkeypatch):
    calls = []

    def fake_request(path, payload=None, method="POST"):
        calls.append((method, path, payload))
        return {"success": True}

    client = MemoryClient("http://x")
    monkeypatch.setattr(client, "_request", fake_request)
    client.update("abc123", {"tags": ["t"], "metadata": {"a": 1}, "content": "不该被接受"})
    method, path, payload = calls[0]
    assert method == "PUT"
    assert path.endswith("/api/memories/abc123")
    assert "content" not in payload                   # 端点只接受 tags/memory_type/metadata


def test_client_rate_calls_native_quality_endpoint(monkeypatch):
    calls = []
    client = MemoryClient("http://x")
    monkeypatch.setattr(client, "_request",
                        lambda path, payload=None, method="POST": calls.append((path, payload)) or {})
    client.rate("abc123", 1, feedback="救场了")
    path, payload = calls[0]
    assert path.endswith("/api/quality/memories/abc123/rate")
    assert payload == {"rating": 1, "feedback": "救场了"}
