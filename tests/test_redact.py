"""脱敏与客户端鉴权（2026-09-22 加）。

两条口径要钉死：
  · **发送前必须盖掉**：蒸馏是唯一把内容送出本机的动作，密钥/邮箱/本机路径不能跟着走
  · **扫描与脱敏同一份规则**：`tools/scrub_check.py` 与 `aml.redact` 不能分叉，
    否则会出现"提交时被拦下、发送时照发不误"的缝
  · **文档里的占位符不算泄漏**：`C:\\Users\\<you>` 这类写法不能把扫描判红
    （实测踩过：判据写成"完全包住"时，CI 与 repo_guard 被自己的文档判红 4 处）
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml import distill, redact  # noqa: E402
from aml.http import MemoryClient, client_for  # noqa: E402

# 测试用的"假敏感串"全部**在运行时拼出来**：字面量写进文件的话，
# `tools/scrub_check.py` 会把这个测试文件自己判成泄漏（它是给真仓库用的守门器，
# 宁可误判也不放过）—— 实测写了字面量版本，扫描报了 7 处命中。
FAKE_KEY = "sk-" + "a" * 24
FAKE_MAIL = "someone@" + "real-domain.cn"
FAKE_WIN = "C:" + "\\Users\\" + "zhangsan\\projects\\x"


# ------------------------------------------------------------------ 检测

def test_find_hits_flags_keys_emails_and_local_paths():
    text = (f'key = "{FAKE_KEY}"\n'
            f"mail: {FAKE_MAIL}\n"
            f"路径 {FAKE_WIN}\n")
    labels = [label for label, _ in redact.find_hits(text)]
    assert "疑似 API key" in labels
    assert "邮箱" in labels
    assert any(label.startswith("绝对路径") for label in labels)


def test_placeholders_are_not_hits():
    """文档里的占位符是**设计上公开**的写法，不能判红（否则 CI 会被自己的文档拦住）。"""
    for line in (r"改成 C:\Users\<you>\... 或 C:\Users\<名>",
                 "见 /home/<user>/x 与 example.com、git@github.com",
                 "提交者是 someone@users.noreply.github.com"):
        assert redact.find_hits(line) == [], line


def _load_scrub():
    """把 tools/scrub_check.py 当模块加载（它不是包的一部分，CI 的 scrub job 也不装包）。"""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "scrub_check.py")
    spec = importlib.util.spec_from_file_location("scrub_check_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scrub_check_shares_the_same_rule_table():
    """扫描器必须 import 同一份规则表（而不是自己再抄一遍正则）。"""
    assert _load_scrub().find_hits is redact.find_hits


def test_scanner_reads_non_utf8_files(tmp_path):
    """非 UTF-8 的文件不能隐身（2026-09-22 补的洞）。

    实证：PowerShell 5.1 的 `>` 重定向写 UTF-16LE，ASCII 之间夹 NUL，
    按 UTF-8 读的话本机路径根本匹配不上 —— 仓库里那个 `err39.txt` 就是这么公开躺了几天的。
    """
    scrub = _load_scrub()
    utf16 = tmp_path / "dump.txt"
    utf16.write_bytes((f"路径 {FAKE_WIN} 结束\n").encode("utf-16"))
    hits = scrub.scan_file(str(utf16), [])
    assert hits, "UTF-16 文件里的本机路径必须被抓到"
    assert "utf-16" in hits[0][0]                      # 报告里要标出"这次是按什么编码读的"
    assert any(label.startswith("绝对路径") for _, _, label, _ in hits)

    # GBK：中文 Windows 的默认写法。中文本身解不出来时，屏蔽词也会一起失效
    gbk = tmp_path / "gbk.txt"
    gbk.write_bytes("客户 ACME 的项目\n".encode("gbk"))
    assert [h for h in scrub.scan_file(str(gbk), ["ACME"]) if h[2] == "屏蔽词"]


# ------------------------------------------------------------------ 脱敏

def test_redact_masks_secrets_and_keeps_the_rest():
    text = f'先设环境变量：export DEEPSEEK_API_KEY="{FAKE_KEY}"\n然后跑 aml sync'
    out, labels = redact.redact(text)
    assert FAKE_KEY not in out
    assert "［已脱敏］" in out
    assert "疑似 API key" in labels
    # 没命中的部分原样保留（脱敏不是把整段删掉）
    assert "然后跑 aml sync" in out


def test_redact_masks_extra_words_and_is_idempotent():
    out, labels = redact.redact("客户 ACME 的项目，客户 ACME 又提了需求", extra_words=["ACME"])
    assert "ACME" not in out and labels == ["屏蔽词"]
    again, _ = redact.redact(out, extra_words=["ACME"])
    assert again == out          # 已经盖过的不再动（重试那一发会走第二次）


def test_safe_transcript_respects_the_switch():
    cfg = cfgmod.load({})
    text = f'token="{FAKE_KEY}"'
    masked, labels, count = distill.safe_transcript(cfg, text)
    assert count == 1 and labels and FAKE_KEY not in masked
    cfg.data["distill"]["redact"] = False
    assert distill.safe_transcript(cfg, text) == (text, [], 0)


def test_api_key_only_reads_explicit_sources(tmp_path, monkeypatch):
    """默认**不**去翻 DSH 的凭据文件；要那条老路必须显式打开开关。"""
    cred = tmp_path / "creds.yaml"
    cred.write_text(f"DEEPSEEK_API_KEY: {FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.delenv("DISTILL_API_KEY", raising=False)
    cfg = cfgmod.load({})
    cfg.data["distill"]["credentials_file"] = str(cred)
    assert distill.api_key(cfg) == ""                       # 默认不读
    cfg.data["distill"]["allow_dsh_credentials"] = True
    assert distill.api_key(cfg) == FAKE_KEY
    cfg.data["distill"]["api_key"] = "sk-explicit"
    assert distill.api_key(cfg) == "sk-explicit"            # 配置优先于凭据文件
    monkeypatch.setenv("DISTILL_API_KEY", "sk-env")
    assert distill.api_key(cfg) == "sk-env"                 # 环境变量优先于一切


# ------------------------------------------------------- 客户端鉴权（第 1 项的客户端侧）

def test_client_for_passes_the_configured_token():
    cfg = cfgmod.load({})
    assert client_for(cfg).token == ""                      # 不配就是老行为
    cfg.data["memory_api_token"] = "s3cret"
    client = client_for(cfg)
    assert isinstance(client, MemoryClient)
    assert client.token == "s3cret"
    assert client.headers()["Authorization"] == "Bearer s3cret"
    # 健康检查也带（否则"加鉴权的部署"连体检都过不去）
    assert client.headers(content_type=False)["Authorization"] == "Bearer s3cret"
