"""入库前脱敏的回归测试（2026-09-24 补）。

背景：本机审计实测，会话原文与知识库素材里的明文凭据会**原样入库**并被检索召回
（库内命中过 MySQL root 口令、公众号 AppSecret、`ghp_` 开头的 token）。
这一道闸门此前是缺的 —— 只有"蒸馏发送前"和"提交扫描前"两道。

注意：测试里的"假敏感串"全部**运行时拼装**。写成字面量的话
`tools/scrub_check.py` 会把这个测试文件自己判成泄漏（test_redact.py 里踩过，报了 7 处）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import ingest, kb, redact  # noqa: E402

MASK = redact.MASK
FAKE_PW = "Very" + "Secret-12345"          # 无引号赋值的形态
FAKE_KEY = "sk-" + "Z" * 24
FAKE_MAIL = "someone@" + "real-domain.cn"


def _rec(text, kind="task"):
    return {"content": text, "tags": [f"kind:{kind}"],
            "metadata": {"kind": kind}, "conversation_id": "session-x"}


def _turn(user, reply=""):
    from aml.adapters.base import Turn
    return Turn(agent="dsh", session="session-abcdef123456", project="demo",
                ts="2026-09-01T00:00:00Z", path="fake.jsonl", user=user, reply=reply, tools=[])


# ------------------------------------------------- 规则表：老规则抓不到的两种形态

def test_rule_table_covers_unquoted_and_chinese_forms():
    """这两条是 2026-09-24 补的：老规则只认"英文关键词 + 引号"。"""
    for text in (f"password: {FAKE_PW}",
                 f"PASSWORD={FAKE_PW}",
                 f"数据库密码是 {FAKE_PW}",
                 f"口令：{FAKE_PW}"):
        out, labels = redact.redact(text)
        assert FAKE_PW not in out, text
        assert labels, text


def test_masked_placeholder_is_not_re_hit():
    """幂等：已经盖过的占位符不能再被中文/无引号规则命中。"""
    once, _ = redact.redact(f"密码是 {FAKE_PW}")
    again, labels = redact.redact(once)
    assert again == once
    assert labels == []


# --------------------------------------------------------------- ingest 侧

def test_redact_records_masks_credentials_and_email():
    recs = [
        _rec(f"我的密码是 {FAKE_PW}，邮箱 {FAKE_MAIL}，key {FAKE_KEY}"),
        _rec("普通对话，没有敏感内容"),
    ]
    info = ingest.redact_records(recs)
    assert info["redacted"] == 1
    assert FAKE_PW not in recs[0]["content"]
    assert FAKE_KEY not in recs[0]["content"]
    assert MASK in recs[0]["content"]
    assert recs[0]["metadata"]["redacted"] is True
    assert recs[0]["metadata"]["redacted_labels"]
    # 不含敏感内容的记录一个字都不该被改
    assert recs[1]["content"] == "普通对话，没有敏感内容"
    assert "redacted" not in recs[1]["metadata"]


def test_redact_records_off_keeps_text():
    recs = [_rec(f"我的密码是 {FAKE_PW}")]
    info = ingest.redact_records(recs, do_redact=False)
    assert info["redacted"] == 0
    assert recs[0]["content"] == f"我的密码是 {FAKE_PW}"


def test_redact_records_extra_words_hits_literal():
    recs = [_rec("这次交付的客户是某某公司")]
    ingest.redact_records(recs, extra_words=["某某公司"])
    assert "某某公司" not in recs[0]["content"]
    assert MASK in recs[0]["content"]


def test_collect_applies_redaction_to_adapter_turns(tmp_path, monkeypatch):
    """端到端：适配器吐出的 turn 一进 collect 就被脱敏（sync/watch 共用这条路）。"""
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    monkeypatch.setattr(ingest, "build", lambda cfg, only_files=None, skip_files=None: [
        type("A", (), {"discover": lambda self: ["fake.jsonl"],
                       "turns": lambda self, _p: iter([_turn(f"账号 root，密码 {FAKE_PW}，帮我连一下")])})()])
    recs = ingest.collect(cfg)
    tasks = [r for r in recs if "kind:task" in r["tags"]]
    assert tasks, "应该至少产出一条 kind:task"
    assert FAKE_PW not in tasks[0]["content"]
    assert tasks[0]["metadata"].get("redacted") is True


def test_collect_redaction_can_be_disabled_by_config(tmp_path, monkeypatch):
    cfg = cfgmod.load({"aml_home": str(tmp_path), "ingest": {"redact": False}})
    monkeypatch.setattr(ingest, "build", lambda cfg, only_files=None, skip_files=None: [
        type("A", (), {"discover": lambda self: ["fake.jsonl"],
                       "turns": lambda self, _p: iter([_turn(f"密码 {FAKE_PW}")])})()])
    recs = ingest.collect(cfg)
    assert any(FAKE_PW in r["content"] for r in recs), "关掉开关时应保持原样"


# ------------------------------------------------------------------- kb 侧

class _CapturingClient:
    def __init__(self):
        self.stored = []

    def store(self, content, tags=None, metadata=None, conversation_id=None):
        self.stored.append(content)
        return {"success": True}


def test_ingest_docs_masks_credentials_in_material(tmp_path, monkeypatch):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    d = cfg.knowledge_dir / "源知识"
    d.mkdir(parents=True)
    (d / "creds.md").write_text(f"数据库 password: {FAKE_PW}\n邮箱 {FAKE_MAIL}", encoding="utf-8")
    fake = _CapturingClient()
    monkeypatch.setattr(kb, "client_for", lambda cfg: fake)
    info = kb.ingest_docs(cfg, dirs=["源知识"])
    assert info["redacted"] >= 1
    joined = "\n".join(fake.stored)
    assert FAKE_PW not in joined
    assert MASK in joined


def test_ingest_docs_redaction_off(tmp_path, monkeypatch):
    cfg = cfgmod.load({"aml_home": str(tmp_path), "ingest": {"redact": False}})
    d = cfg.knowledge_dir / "源知识"
    d.mkdir(parents=True)
    (d / "creds.md").write_text(f"password: {FAKE_PW}", encoding="utf-8")
    fake = _CapturingClient()
    monkeypatch.setattr(kb, "client_for", lambda cfg: fake)
    info = kb.ingest_docs(cfg, dirs=["源知识"])
    assert info["redacted"] == 0
    assert FAKE_PW in "\n".join(fake.stored)
