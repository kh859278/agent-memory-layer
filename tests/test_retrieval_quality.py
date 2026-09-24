"""检索质量的回归测试（2026-09-24 补）。

修的两个实测缺陷：
  1. **`JUNK_TAGS` 是死代码**：`kind:task/kind:reply`（原始对话）注释写着"只作最后兜底"，
     但从来没有代码执行这条约束。实测查"powershell 编码 乱码"，语义 top-5 里 3 条是会话流水。
  2. **近义条目吃光预算**：查"事件驱动 播报 轮询"，top-5 全是"轮询≠事件驱动"的措辞变体，
     P2 只给 3 条 / 600 字，等于整段预算只讲了一句话。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml.retrieval import Retriever  # noqa: E402


class FakeClient:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, n_results=10):
        return list(self.hits)

    def health(self):
        return {"status": "healthy"}


def mem(content, tags, score_iso="2026-08-01T00:00:00Z", src=None):
    # content_hash 用**全文**：早先写成 content[:16] 时，两条只差一个字的条目
    # 会因为 hash 前缀相同被当成同一条（走 seen 分支而不是相似度分支），测试就假过了。
    meta = {"src_session": src} if src else {}
    return {"content": content, "tags": tags, "created_at_iso": score_iso,
            "content_hash": content, "metadata": meta}


KNOW = ["kind:knowledge", "domain:tooling"]
JUNK = ["agent:dsh", "kind:reply", "project:demo"]


def test_session_flow_is_deferred_behind_knowledge(tmp_path):
    """会话流水即使分数更高，也不许抢走知识层的注入位。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    hits = [
        (0.95, mem("补 BOM 就好了（用了工具：pwsh）", JUNK)),          # 会话流水，分更高
        (0.94, mem("我的新脚本没 BOM 所以乱码（用了工具：pwsh）", JUNK)),
        (0.93, mem("又是编码问题（用了工具：pwsh）", JUNK)),
        (0.90, mem("控制台乱码先查编码，不要急着重写数据", KNOW)),
        (0.89, mem("PowerShell 5.1 无 BOM 的 UTF-8 脚本会被按 GBK 读", KNOW)),
        (0.88, mem("重定向产物先判 BOM 再解码", KNOW)),
    ]
    r = Retriever(cfg, client=FakeClient(hits)).search("编码乱码", phase="P2", n=3)
    assert len(r.lines) == 3
    assert all("kind:reply" not in ln for ln in r.lines)              # 行里不该出现会话流水的来源标
    assert all("补 BOM 就好了" not in ln for ln in r.lines)
    assert r.diag["junk_deferred"] == 3


def test_session_flow_is_used_when_nothing_else_exists(tmp_path):
    """知识层没有命中时，会话流水仍要能兜底（不能变成"什么都查不到"）。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    hits = [(0.90, mem("这是原始对话里的一句话", JUNK)),
            (0.88, mem("另一句原始对话", JUNK))]
    r = Retriever(cfg, client=FakeClient(hits)).search("只有会话流水", phase="P2", n=3)
    assert len(r.lines) == 2
    assert "会话流水" in r.layers


def test_near_duplicate_is_skipped_and_slot_filled_by_distinct(tmp_path):
    """近重复不占位：位子让给下一条真正不同的内容。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    a = "写工具要求先读后写，被拒绝时改用 shell 临时脚本落盘"
    a2 = "写工具要求先读后写，被拒绝时改用 shell 临时脚本写盘"        # 只差一个字
    b = "并发写同一文件时先加锁，锁只覆盖提交不覆盖工作树"
    hits = [(0.95, mem(a, KNOW)), (0.94, mem(a2, KNOW)), (0.93, mem(b, KNOW))]
    r = Retriever(cfg, client=FakeClient(hits)).search("写工具被拒", phase="P2", n=2)
    texts = " ".join(r.lines)
    assert a[:12] in texts
    assert b[:12] in texts, "近重复被跳过之后，下一条不同的内容应该补上这个位子"
    assert r.diag["dedup_skipped"] == 1


def test_retrieval_dedup_can_be_disabled(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path), "retrieval": {"dedup_sim": 0}})
    a = "写工具要求先读后写，被拒绝时改用 shell 临时脚本落盘"
    a2 = "写工具要求先读后写，被拒绝时改用 shell 临时脚本写盘"
    hits = [(0.95, mem(a, KNOW)), (0.94, mem(a2, KNOW))]
    r = Retriever(cfg, client=FakeClient(hits)).search("写工具被拒", phase="P2", n=2)
    assert len(r.lines) == 2                     # 关掉之后两条都进
    assert r.diag["dedup_skipped"] == 0


def test_empty_result_does_not_claim_library_is_empty_when_only_junk(tmp_path):
    """全部候选都是会话流水、且都没到阈值时，解释里不能再写"库可能是空的"。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    hits = [(0.50, mem("一条分数很低的原始对话", JUNK))]
    r = Retriever(cfg, client=FakeClient(hits)).search("查不到的东西", phase="P3")
    assert r.empty
    text = r.render()
    assert "会话流水" in text
    assert "库可能是空的" not in text


def test_same_source_is_capped_and_slots_filled_by_others(tmp_path):
    """同一会话反复蒸馏出的多句同义忠告，不许占满注入位。

    实测依据：查"事件驱动 播报 轮询"，top-5 全部来自 session-164d2fd6，
    两两逐字相似度最高仅 0.103（词面阈值抓不到），只能按"同源"限流。
    """
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    hits = [
        (0.90, mem("轮询要说明延迟上限", KNOW, src="session-164d2fd6")),
        (0.89, mem("别把轮询说成实时推送", KNOW, src="session-164d2fd6")),
        (0.88, mem("通知要先确认交付渠道", KNOW, src="session-164d2fd6")),
        (0.85, mem("并发写同一文件要先加锁", KNOW, src="session-a8a0e197")),
        (0.84, mem("状态枚举变更要审计下游", KNOW, src="session-99xx")),
    ]
    r = Retriever(cfg, client=FakeClient(hits)).search("轮询 触发 语义", phase="P2", n=3)
    picked = " ".join(r.lines)
    assert r.diag["same_source_skipped"] >= 1
    assert "并发写同一文件要先加锁" in picked, "同源被限之后，位子要让给别的来源"
    assert len(r.lines) == 3


def test_same_source_cap_is_soft_when_no_alternatives(tmp_path):
    """限流是软约束：只有同源候选时，该给的还是要给出来。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    hits = [(0.9 - i / 100, mem(f"同一个会话的第 {i} 条经验", KNOW, src="session-164d2fd6"))
            for i in range(3)]
    r = Retriever(cfg, client=FakeClient(hits)).search("只有同源", phase="P2", n=3)
    assert len(r.lines) == 3, "不能因为限流把查得到的内容变成给不出来"


def test_per_source_max_can_be_disabled(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path), "retrieval": {"per_source_max": 0}})
    hits = [(0.90, mem("轮询要说明延迟上限", KNOW, src="s1")),
            (0.89, mem("别把轮询说成实时推送", KNOW, src="s1")),
            (0.85, mem("并发写同一文件要先加锁", KNOW, src="s2"))]
    r = Retriever(cfg, client=FakeClient(hits)).search("轮询 触发", phase="P2", n=2)
    assert r.diag["same_source_skipped"] == 0
    assert "别把轮询说成实时推送" in " ".join(r.lines)
