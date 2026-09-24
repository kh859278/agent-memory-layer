"""溯源键的回归测试（2026-09-24 补）。

背景：`src_session` 历史上写进去过两种形态（带 `session-` 前缀的 36 位全 id 与纯 8 位短 hex），
直接按字符串比对时 join 成功率看着只有 2%，**归一到核心 8 位后其实是 99.7%**。
所以写入侧要统一成规范短键，并另存一份全 id 供精确回溯。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import distill  # noqa: E402


def test_session_key_normalizes_both_historical_forms():
    full = "session-fbd676be-9013-4d56-a590-2f235106bd1f"
    assert distill.session_key(full) == "fbd676be"
    assert distill.session_key("fbd676be") == "fbd676be"
    assert distill.session_key(full) == distill.session_key("fbd676be")   # 两种形态归一后相等
    assert distill.session_key("") == ""
    assert distill.session_key(None) == ""


class _CapturingClient:
    def __init__(self):
        self.calls = []

    def store(self, content, tags=None, metadata=None, conversation_id=None):
        self.calls.append({"content": content, "tags": tags, "metadata": metadata,
                           "conversation_id": conversation_id})
        return {"success": True}


def _item(session_id):
    return {"session_id": session_id, "short": session_id.replace("session-", "")[:8],
            "agent": "dsh"}


def test_write_entries_stores_canonical_short_key_and_full_id(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    fake = _CapturingClient()
    full = "session-fbd676be-9013-4d56-a590-2f235106bd1f"
    n = distill.write_entries(cfg, [{"title": "T", "body": "B", "domain": "d", "type": "pattern"}],
                              _item(full), client=fake)
    assert n == 1
    call = fake.calls[0]
    # 短键与 ingest 写的标签 session:<8位> 对齐
    assert call["metadata"]["src_session"] == "fbd676be"
    assert "src_session:fbd676be" in call["tags"]
    # 全 id 另存一份，供精确回溯
    assert call["metadata"]["src_session_id"] == full
    assert call["metadata"]["src_agent"] == "dsh"


def test_short_id_input_is_not_double_truncated(tmp_path):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    fake = _CapturingClient()
    distill.write_entries(cfg, [{"title": "T", "body": "B"}], _item("fbd676be"), client=fake)
    assert fake.calls[0]["metadata"]["src_session"] == "fbd676be"
