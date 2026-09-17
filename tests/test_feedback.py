"""质量反馈测试：可靠度算法、打点、同档位重排（全部用假 client）。

设计取舍写在测试里，避免以后被"改回去"：
  · 没有使用数据 → 可靠度是 None、排序系数 1.0（**不惩罚新记忆**）
  · 1 次成功 ≠ 满分（拉普拉斯平滑，1/3）
  · 反馈只改**同档位内的顺序**，不改分数门槛（否则新记忆会被挤出结果）
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import config as cfgmod  # noqa: E402
from aml import feedback  # noqa: E402
from aml.retrieval import Retriever  # noqa: E402


class FakeClient:
    def __init__(self, memories):
        self.memories = {m["content_hash"]: dict(m) for m in memories}
        self.updated = []
        self.rated = []
        self.search_hits = []

    # 反馈用
    def get(self, content_hash):
        memory = self.memories.get(content_hash)
        if not memory:
            raise KeyError(content_hash)
        return memory

    def iter_memories(self, tag=None, max_pages=500):
        return list(self.memories.values())

    def update(self, content_hash, updates):
        self.updated.append((content_hash, updates))
        meta = self.memories[content_hash].setdefault("metadata", {})
        meta.update(updates.get("metadata") or {})
        return {"success": True}

    def rate(self, content_hash, rating, feedback=""):
        self.rated.append((content_hash, rating, feedback))
        return {"success": True}

    # 检索用
    def search(self, query, n_results=10):
        return list(self.search_hits)


def memory(hash_, meta=None, tags=None, content="一条经验"):
    return {"content_hash": hash_, "content": content, "tags": tags or ["kind:knowledge"],
            "created_at_iso": "2026-09-01T00:00:00Z", "metadata": dict(meta or {})}


# --------------------------------------------------------------- 算法

def test_reliability_needs_data_and_is_smoothed():
    assert feedback.reliability({}) is None                       # 没数据 → None（不惩罚）
    assert feedback.reliability({"success_count": 1}) == 0.667    # 1 次成功不是满分
    assert feedback.reliability({"success_count": 17, "failure_count": 3}) == 0.818
    assert feedback.reliability({"success_count": 3, "failure_count": 17}) == 0.182


def test_rank_factor_is_neutral_without_data_and_bounded():
    assert feedback.rank_factor({}) == 1.0                        # 没数据不惩罚
    assert feedback.rank_factor({"success_count": 10}) > 1.0      # 被证实有效 → 提升
    assert feedback.rank_factor({"failure_count": 10}) < 1.0      # 屡次失败 → 降权
    assert feedback.rank_factor({"failure_count": 99}) >= 0.85    # 保底：不消失
    assert feedback.rank_factor({"success_count": 99}) <= 1.15    # 封顶：别让一条记忆碾压一切
    assert feedback.rank_factor({"success_count": 1, "failure_count": 1}) == 1.0   # 五五开 = 中性


# --------------------------------------------------------------- 打点

def test_record_increments_counters(tmp_path):
    client = FakeClient([memory("h1", {"title": "标题"})])
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    info = feedback.record(cfg, "h1", "worked", note="救场了", client=client, log=lambda *_: None)
    assert info["ok"] is True
    meta = info["meta"]
    assert meta["usage_count"] == 1 and meta["success_count"] == 1
    assert meta["last_used_at"] and meta["last_note"] == "救场了"
    assert client.updated[0][0] == "h1"
    # 同时打服务的原生质量评分（rating: worked→1 / used→0 / failed→-1）
    assert client.rated == [("h1", 1, "救场了")]


def test_record_failed_sets_failure_fields(tmp_path):
    client = FakeClient([memory("h1")])
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    meta = feedback.record(cfg, "h1", "failed", client=client, log=lambda *_: None)["meta"]
    assert meta["failure_count"] == 1 and meta["last_failed_at"]
    assert feedback.reliability(meta) == 0.333


def test_record_rejects_unknown_outcome_and_missing_hash(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    client = FakeClient([memory("h1")])
    try:
        feedback.record(cfg, "h1", "也许有效", client=client, log=lambda *_: None)
    except ValueError as e:
        assert "outcome" in str(e)
    else:  # pragma: no cover
        raise AssertionError("非法 outcome 应当报错")

    missing = feedback.record(cfg, "nope", "used", client=client, log=lambda *_: None)
    assert missing["ok"] is False and "找不到" in missing["error"]


def test_verify_marks_timestamp_and_extends_review(tmp_path):
    client = FakeClient([memory("h1", {"review_after": "2026-01-01"})])
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    info = feedback.verify(cfg, "h1", days=180, client=client, log=lambda *_: None)
    assert info["ok"] is True and info["verified_at"]
    assert info["review_after"] > "2026-01-01"
    assert client.memories["h1"]["metadata"]["last_verified_at"]


# ------------------------------------------------- 排序（同档位内重排）

def test_reliability_reranks_within_same_tier(tmp_path):
    """两条都过阈值：被证实有效的**能反超**分数略高但没用过的那条。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    fresh = memory("fresh", {"title": "没用过的新经验"}, content="新的那条")
    proven = memory("proven", {"success_count": 9, "failure_count": 1}, content="被证实的那条")
    client = FakeClient([fresh, proven])
    client.search_hits = [(0.90, fresh), (0.86, proven)]

    result = Retriever(cfg, client=client).search("q", phase="P2", allow_repeat=True)
    assert len(result.lines) == 2                    # 两条都还在（门槛只看原始分）
    assert "被证实的那条" in result.lines[0]          # 0.86 × 1.10 > 0.90 × 1.00
    assert "rel 0.83" in result.lines[0]


def test_low_reliability_entry_is_not_dropped(tmp_path):
    """屡次失败的记忆仍然可被检索到（只降序不删除）——否则会丢掉"这条路走不通"的信息。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    bad = memory("bad", {"success_count": 0, "failure_count": 5})
    client = FakeClient([bad])
    client.search_hits = [(0.85, bad)]
    result = Retriever(cfg, client=client).search("q", phase="P2", allow_repeat=True)
    assert len(result.lines) == 1
    assert "rel 0.14" in result.lines[0]              # 前缀里显示可靠度（让人自己判断）


def test_no_reliability_shown_when_no_data(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    plain = memory("plain", {"title": "新记忆"})
    client = FakeClient([plain])
    client.search_hits = [(0.9, plain)]
    line = Retriever(cfg, client=client).search("q", phase="P2", allow_repeat=True).lines[0]
    assert "rel" not in line                          # 没数据就不显示，避免噪声
