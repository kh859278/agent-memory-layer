"""领域名归一的回归测试（2026-09-24 补）。

背景：库里出现 12 条 `<ktype>/<domain>` 形态的 domain（`checklist/env-setup`、
`pitfall/data-pipeline`、`tooling/web-deploy`…）。带斜杠的 domain 映射不成
`沉淀/<domain>.md`，这些条目**只在机器面存在、人面永远看不到**。
根因很可能是 distill 的 PROMPT 里用了斜杠分隔的示例（`env-windows/data-scraping/agent-workflow`），
模型照着抄了 —— PROMPT 与写入路径都已修。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import distill  # noqa: E402
from aml.text import domain_of  # noqa: E402


def test_domain_of_folds_separators():
    assert domain_of("checklist/env-setup") == "checklist-env-setup"
    assert domain_of("agent-workflow\\timezone") == "agent-workflow-timezone"
    assert domain_of("  a  b  ") == "a-b"
    assert domain_of("a//b") == "a-b"
    assert domain_of("") == "general"
    assert domain_of(None) == "general"
    assert domain_of("-x-") == "x"
    # 正常名字不许被改坏
    for good in ("env-windows", "data-scraping", "agent-workflow", "写作"):
        assert domain_of(good) == good


def test_prompt_does_not_show_slash_separated_domains():
    """PROMPT 里不能再出现 `a/b/c` 这种示例 —— 那就是畸形 domain 的来源。"""
    assert "env-windows/data-scraping" not in distill.PROMPT
    assert "不许出现斜杠" in distill.PROMPT


class _CapturingClient:
    def __init__(self):
        self.calls = []

    def store(self, content, tags=None, metadata=None, conversation_id=None):
        self.calls.append({"tags": tags, "metadata": metadata})
        return {"success": True}


def test_write_entries_normalizes_domain_from_model(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    fake = _CapturingClient()
    distill.write_entries(
        cfg,
        [{"title": "T", "body": "B", "domain": "checklist/env-setup", "type": "checklist"}],
        {"session_id": "session-abcdef123456", "short": "abcdef12", "agent": "dsh"},
        client=fake)
    call = fake.calls[0]
    assert "domain:checklist-env-setup" in call["tags"]
    assert call["metadata"]["domain"] == "checklist-env-setup"
    assert not any("/" in t for t in call["tags"])


def test_mcp_store_normalizes_domain_tag(tmp_path):
    from aml.mcp_server import Server
    cfg = cfgmod.load({"aml_home": str(tmp_path)})

    class _C:
        def __init__(self):
            self.saved = None

        def store(self, content, tags, metadata, conversation_id=None):
            self.saved = list(tags)
            return {"success": True}

    fake = _C()
    server = Server(cfg, client=fake)
    # 走真正的 JSON-RPC 形状（tools/call），别私有地调内部函数
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "store",
                              "arguments": {"content": "正文",
                                            "tags": ["kind:knowledge",
                                                     "domain:pitfall/data-pipeline"]}}})
    assert fake.saved is not None, "store 应该被调用"
    assert "domain:pitfall-data-pipeline" in fake.saved
    assert not any(t.startswith("domain:") and "/" in t for t in fake.saved)


def test_mcp_store_keeps_only_one_domain_tag(tmp_path):
    """多个 domain 标签只留第一个：镜像与 metadata 本来就只认第一个，
    其余的会变成"只在机器面"的幽灵（实测 23 条）。"""
    from aml.mcp_server import Server
    cfg = cfgmod.load({"aml_home": str(tmp_path)})

    class _C:
        def __init__(self):
            self.tags = None
            self.meta = None

        def store(self, content, tags, metadata, conversation_id=None):
            self.tags, self.meta = list(tags), dict(metadata)
            return {"success": True}

    fake = _C()
    Server(cfg, client=fake).handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "store",
                    "arguments": {"content": "正文",
                                  "tags": ["kind:knowledge", "domain:windows",
                                           "domain:powershell", "ktype:pitfall"]}}})
    assert [t for t in fake.tags if t.startswith("domain:")] == ["domain:windows"]
    assert fake.meta["dropped_domain_tags"] == ["domain:powershell"]
    assert "ktype:pitfall" in fake.tags          # 非 domain 标签不受影响
