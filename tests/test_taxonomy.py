"""领域词表：把模型自造的 domain 收敛到 ≤32 个（2026-11-20：实测长出 823 个）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import distill, taxonomy  # noqa: E402


def test_vocab_is_small_and_unique():
    assert len(taxonomy.VOCAB) == len(set(taxonomy.VOCAB))
    assert len(taxonomy.VOCAB) <= 32
    assert "misc" in taxonomy.VOCAB


def test_exact_vocab_kept():
    for name in ("agent-workflow", "env-windows", "data-scraping"):
        assert taxonomy.canon(name) == name


def test_aliases_fold():
    assert taxonomy.canon("agent-evaluation") == "agent-eval"
    assert taxonomy.canon("knowledge-management") == "agent-memory"
    assert taxonomy.canon("wechat-publishing") == "content-ops"


def test_keywords_beat_prefix():
    # domain 名像 agent-*，但正文讲的是沙箱权限 —— 关键词优先
    assert taxonomy.canon("agent-foo", "沙箱里权限被拒", "") == "env-sandbox"


def test_prefix_fallback():
    assert taxonomy.canon("env-whatever", "无关键词的正文", "") == "env-windows"
    assert taxonomy.canon("random-thing", "看这里", "") == "misc"


def test_ktype_prefix_is_stripped_before_mapping():
    # 历史写法 `checklist/env-setup` → domain_of 折成 checklist-env-setup → 去类型前缀再映射
    assert taxonomy.canon("checklist/env-setup") == "tooling"
    assert taxonomy.canon("pitfall/data-pipeline") == "data-quality"


def test_empty_domain_never_empty():
    assert taxonomy.canon("") == "misc"
    assert taxonomy.canon(None) == "misc"


def test_sink_markdown_uses_canonical_domain(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path), "knowledge_dir": str(tmp_path / "kb")})
    distill.sink_markdown(cfg, [{"title": "t", "body": "b", "domain": "agent-evaluation",
                                 "type": "pitfall", "confidence": "high"}],
                          {"agent": "dsh", "short": "abc"})
    assert (tmp_path / "kb" / "沉淀" / "agent-eval.md").is_file()
    assert not (tmp_path / "kb" / "沉淀" / "agent-evaluation.md").exists()
