"""更正条约定（2026-09-26 加）的回归测试。

要钉死：
  1. 更正条声明了 `metadata.supersedes` 时，被取代的旧条**不进上下文**
  2. **没声明**时两条都会进来 —— 这条对照是证明"链接才是起作用的原因"，
     因为实测两者的词面 Jaccard 只有 0.103，相似度阈值永远抓不到
  3. `supersedes` 允许写 hash 前缀
  4. `distill.write_correction()` 落库的形态：标题、ktype、supersedes、正文三要素
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import distill  # noqa: E402
from aml.retrieval import Retriever  # noqa: E402


class FakeClient:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, n_results=10):
        return list(self.hits)

    def health(self):
        return {"status": "healthy"}


class RecordingClient:
    """只记下 store() 收到了什么，不落库。"""

    def __init__(self):
        self.calls = []

    def store(self, content, tags=None, metadata=None, conversation_id=None):
        self.calls.append({"content": content, "tags": tags, "metadata": metadata,
                           "conversation_id": conversation_id})
        return {"success": True}


def mem(content, tags, iso="2026-08-01T00:00:00Z", meta=None):
    return {"content": content, "tags": tags, "created_at_iso": iso,
            "content_hash": content, "metadata": meta or {}}


KNOW = ["kind:knowledge", "domain:env-sandbox", "ktype:pitfall"]

STALE = "受限沙箱中写系统临时目录会被拒。应将 TMP/TEMP 指向工作区可写路径即可。"
FIXED = ("受限沙箱里 tempfile.mkdtemp 建的一次性目录写不进去。关键更正：把 TMPDIR 指向"
         "工作区内仍然失败；真因是 mkdtemp 内部用 0o700 建目录。做法：用默认 mode 建目录。")


def test_correction_supersedes_stale_entry(tmp_path):
    """更正条写明取代谁 → 旧条不进上下文。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    hits = [
        (0.90, mem(FIXED, KNOW + ["ktype:correction"],
                   iso="2026-09-24T00:00:00Z", meta={"supersedes": STALE})),
        (0.88, mem(STALE, KNOW, iso="2026-09-23T00:00:00Z")),
    ]
    r = Retriever(cfg, client=FakeClient(hits)).search("沙箱 临时目录 写不进去", phase="P2", n=3)
    assert len(r.lines) == 1, r.lines
    assert "0o700" in r.lines[0]
    assert STALE not in "\n".join(r.lines)
    assert r.diag["superseded_dropped"] == 1


def test_without_supersedes_both_entries_survive(tmp_path):
    """对照：没有 supersedes 链接时，新旧两条会一起进上下文。

    这条不是凑数 —— 它证明"新压过旧"靠的是显式链接，而不是相似度：
    这两条正文的 Jaccard 只有 0.103，near-dup 阈值 0.85 根本不会分组。
    """
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    hits = [
        (0.90, mem(FIXED, KNOW + ["ktype:correction"], iso="2026-09-24T00:00:00Z")),
        (0.88, mem(STALE, KNOW, iso="2026-09-23T00:00:00Z")),
    ]
    r = Retriever(cfg, client=FakeClient(hits)).search("沙箱 临时目录 写不进去", phase="P2", n=3)
    assert len(r.lines) == 2
    assert r.diag["superseded_dropped"] == 0


def test_supersedes_accepts_hash_prefix(tmp_path):
    """只写 hash 前缀也该认得出来（人手抄 hash 不会抄全）。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    stale_hash = "effa7b47deadbeef" + STALE
    hits = [
        (0.90, mem(FIXED, KNOW + ["ktype:correction"],
                   iso="2026-09-24T00:00:00Z", meta={"supersedes": "effa7b47"})),
        (0.88, {"content": STALE, "tags": KNOW, "created_at_iso": "2026-09-23T00:00:00Z",
                "content_hash": stale_hash, "metadata": {}}),
    ]
    r = Retriever(cfg, client=FakeClient(hits)).search("沙箱 临时目录", phase="P2", n=3)
    assert len(r.lines) == 1 and r.diag["superseded_dropped"] == 1


def test_write_correction_shape(tmp_path):
    """write_correction 的落库形态：标题、ktype、supersedes、正文三要素都在。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    rec = RecordingClient()
    res = distill.write_correction(
        cfg, subject="沙箱临时目录 0o700",
        was="把 TMP 指到工作区就行", now="要避开 mkdtemp 的 0o700，用默认 mode 建目录",
        evidence="2026-09-24 实测 0o777 OK / 0o700 DENY",
        supersedes="effa7b47", domain="env-sandbox", client=rec)
    assert res["ok"] is True and len(rec.calls) == 1
    call = rec.calls[0]
    assert call["metadata"]["title"] == "更正：沙箱临时目录 0o700"
    assert "ktype:correction" in call["tags"]
    assert "kind:knowledge" in call["tags"] and "reusable:true" in call["tags"]
    assert call["metadata"]["supersedes"] == "effa7b47"
    body = call["content"]
    assert body.startswith("【更正：沙箱临时目录 0o700】")
    assert "原来声称" in body and "现在为真" in body and "依据" in body

def test_search_tool_requires_visible_credit():
    """署名约定：说明里必须写清「用了要署名、没出力的别署名」。

    为什么要规定得这么细：排序里的"越用越准"要靠「哪条真被用上」来打点，
    而署名是这件事唯一的证据来源；给没贡献的记忆署名，等于往这份证据里掺假。
    """
    from aml import mcp_server

    desc = [t for t in mcp_server.TOOLS if t["name"] == "search"][0]["description"]
    assert "署名" in desc, desc
    assert "没出力" in desc, desc
