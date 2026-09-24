"""人面镜像重建的回归测试（2026-09-24 补）。

两个实测缺陷：
  1. `rebuild_markdown` 原先写死 `search_by_tag(n=1000)` —— 库内 1 123 条时只渲染 1000 条，
     静默少 11%（人面与库不一致，正是"两副面孔"承诺的反面）。
  2. 重建只写不删 —— 去重/改 domain 之后，旧领域文件变成孤儿，人面会显示库里没有的条目。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import distill  # noqa: E402


def _mem(domain, title, body="正文"):
    return {"content": f"【{title}】{body}", "tags": ["kind:knowledge", f"domain:{domain}"],
            "metadata": {"domain": domain, "title": title, "ktype": "pattern"}, "created_at_iso": "2026-09-01T00:00:00Z"}


class _PagedClient:
    """模拟分页端点：每页 100，共 total 条。"""

    def __init__(self, total):
        self.items = [_mem("bulk", f"条{i}") for i in range(total)]

    def iter_memories(self, tag=None, max_pages=500):
        for i in range(0, len(self.items), 100):
            yield from self.items[i:i + 100]

    def search_by_tag(self, tags, match_all=False, n=20):   # pragma: no cover - 不该被走到
        raise AssertionError("有分页端点时不该退回 search_by_tag")


def test_rebuild_renders_more_than_the_old_1000_cap(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    info = distill.rebuild_markdown(cfg, client=_PagedClient(1123))
    assert info["entries"] == 1123, "超过 1000 条时不能再被截断"
    text = (cfg.knowledge_dir / "沉淀" / "bulk.md").read_text(encoding="utf-8")
    assert "共 1123 条" in text


class _FakeClient:
    def iter_memories(self, tag=None, max_pages=500):
        yield _mem("keep", "保留的")
        yield {"content": "别的", "tags": ["kind:task"], "metadata": {}}   # 非知识条：要滤掉

    def search_by_tag(self, tags, match_all=False, n=20):   # pragma: no cover
        raise AssertionError("分页已可用")


def test_rebuild_removes_stale_generated_files_only(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    sink = cfg.knowledge_dir / "沉淀"
    sink.mkdir(parents=True, exist_ok=True)
    # 上一轮生成、这一轮已不存在的领域文件
    (sink / "old-domain.md").write_text(
        "# 沉淀：old-domain\n\n> 由记忆层重建（2026-09-01 00:00），共 1 条。\n\n## x\n\n正文\n",
        encoding="utf-8")
    # 手写文件（不能删）
    (sink / "AGENTS.md").write_text("# 沉淀层说明\n\n手写内容\n", encoding="utf-8")

    info = distill.rebuild_markdown(cfg, client=_FakeClient())
    assert info["entries"] == 1                       # 非知识条被滤掉
    assert (sink / "keep.md").is_file()
    assert not (sink / "old-domain.md").exists(), "孤儿领域文件应被清掉"
    assert (sink / "AGENTS.md").is_file(), "手写文件绝不能被删"
    assert info["removed"] == ["old-domain.md"]


def test_rebuild_falls_back_when_paging_unavailable(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})

    class _NoPaging:
        def iter_memories(self, tag=None, max_pages=500):
            raise RuntimeError("端点不存在")

        def search_by_tag(self, tags, match_all=False, n=20):
            return [_mem("fallback", "退回路径")]

    info = distill.rebuild_markdown(cfg, client=_NoPaging())
    assert info["entries"] == 1
    assert (cfg.knowledge_dir / "沉淀" / "fallback.md").is_file()
