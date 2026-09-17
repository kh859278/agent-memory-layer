"""`patrol diff` 的测试：能力信号识别 + 差异报告 + 门禁退出码。

重点在"不误报、不漏报"：
  · 普通散文里的 "curl 一下" 不该被判成网络能力（那是讨论，不是指令）
  · 上游**新增**的网络调用/shell/读密钥必须被标成高风险
  · 只有正文润色（没有新能力）时不该报风险
"""
from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0] + "src")

from aml import config as cfgmod  # noqa: E402
from aml.patrol import diff as diff_mod  # noqa: E402


def write_skill(root, name, body, extra=None):
    import os
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(body)
    for rel, text in (extra or {}).items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
    return path


# ------------------------------------------------------------- 能力信号

def test_scan_signals_finds_high_risk_capabilities():
    text = (
        "运行 `curl https://collect.example.com/report` 上传结果。\n"
        "然后 bash -c 'rm -rf ./dist' 并读取 ~/.ssh/id_rsa 用于签名。\n"
        "最后 git push origin main。\n"
    )
    found = diff_mod.scan_signals(text)
    assert "network" in found and any("https://collect" in x for x in found["network"])
    assert "shell" in found
    assert "secrets" in found
    assert "filesystem-write" in found
    assert "git-write" in found


def test_scan_signals_is_quiet_on_plain_prose():
    found = diff_mod.scan_signals("这个技能会帮你整理笔记，把要点摘出来并生成一份清单。")
    assert found == {}


def test_markdown_formatting_is_not_a_capability():
    """实测踩到的误报：引用块 `> xxx` 被判成写文件、行内反引号被判成 shell。

    治理工具一旦狼来了就没人看 —— 所以文档排版符号不算能力，只有可执行调用才算。
    """
    doc = (
        "> 这是引用块，不是重定向。\n"
        "使用 `SKILL.md` 里的 `front-matter` 字段，比如 `name: foo`。\n"
        "A && B 这种写法在这里只是文字说明。\n"
        "参考 [文档](https://example.com/docs) 了解更多。\n"
    )
    found = diff_mod.scan_signals(doc)
    assert "shell" not in found
    assert "filesystem-write" not in found
    assert "network" not in found            # 裸链接不算"发起网络请求"
    assert "url" in found                    # 但会作为"提到外部链接"被留意到


def test_real_invocations_still_detected():
    found = diff_mod.scan_signals("```bash\ncurl -X POST https://x.example/hook\n```\n"
                                  "然后 subprocess.run(['rm', '-rf', 'dist'])")
    assert "network" in found and "shell" in found


def test_signal_diff_separates_new_from_existing():
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        local = write_skill(tmp, "local", "读取配置并输出报告。\ncurl https://old.example.com\n")
        other = write_skill(tmp, "other", "读取配置并输出报告。\ncurl https://old.example.com\n"
                                          "curl https://new.example.com\n")
        result = diff_mod.signal_diff(local, other)
        assert any("new.example.com" in x for x in result["new"]["network"])
        assert not any("new.example.com" in x for x in result["gone"].get("network", []))
        assert os.path.isdir(other)


# ------------------------------------------------------------- 差异报告

def test_diff_skill_reports_files_and_risk():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        local = write_skill(tmp, "v1", "# 部署\n执行 npm install 然后启动。\n")
        other = write_skill(tmp, "v2", "# 部署\n执行 pnpm install 然后启动。\n"
                                          "部署前先 curl https://ops.example.com/hook\n",
                            extra={"reference.md": "新的参考文档"})
        item = diff_mod.diff_skill(local, other)
        assert item["files"]["added"] == ["reference.md"]
        assert "SKILL.md" in item["files"]["changed"]
        assert any("pnpm" in row for row in item["files"]["lines"]) or item["files"]["lines"]
        assert "network" in item["signals"]["new"]
        assert item["new_high_risk"] == ["network"]


def test_diff_skill_marks_cosmetic_change_as_not_risky():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        local = write_skill(tmp, "v1", "# 说明\n这是一段解释文字，没有命令。\n")
        other = write_skill(tmp, "v2", "# 说明\n这段解释文字改得更清楚了，仍然没有命令。\n")
        item = diff_mod.diff_skill(local, other)
        assert item["files"]["changed"] == ["SKILL.md"]
        assert item["new_high_risk"] == []
        assert item["signals"]["new"] == {}


# ------------------------------------------------- 收集（走暂存区，不联网）

def make_cfg(tmp_path, skill_root):
    return cfgmod.load({
        "aml_home": str(tmp_path),
        "patrol": {"skill_roots": [{"name": "test", "path": str(skill_root),
                                    "mirror_to": "技能原始/test"}]},
    })


def stage_pending(cfg, name, body):
    import os
    pending = cfg.state_dir / "patrol" / "_pending" / f"{name}-abc1234"
    os.makedirs(pending, exist_ok=True)
    (pending / "SKILL.md").write_text(body, encoding="utf-8")
    return pending


def test_collect_uses_pending_without_network(tmp_path):
    skills_root = tmp_path / "skills"
    write_skill(str(skills_root), "deploy", "# 部署\n执行 npm install。\n")
    cfg = make_cfg(tmp_path, skills_root)
    stage_pending(cfg, "deploy", "# 部署\n执行 npm install。\n先 curl https://ops.example.com/hook\n")

    report = diff_mod.collect(cfg, log=lambda *_: None)
    assert report["source"] == "pending"
    assert len(report["skills"]) == 1
    item = report["skills"][0]
    assert item["name"] == "deploy"
    assert item["new_high_risk"] == ["network"]
    assert diff_mod.risky(report) is True

    text = diff_mod.render(report)
    assert "新增能力信号" in text and "高风险" in text
    assert "accept deploy" in text          # 结论里给出下一步命令


def test_collect_reports_when_nothing_to_review(tmp_path):
    cfg = make_cfg(tmp_path, tmp_path / "skills")
    report = diff_mod.collect(cfg, log=lambda *_: None)
    assert report["skills"] == []
    assert any("_pending" in note for note in report["notes"])
    assert "没有" in diff_mod.render(report)


def test_render_flags_cosmetic_only_change(tmp_path):
    skills_root = tmp_path / "skills"
    write_skill(str(skills_root), "notes", "# 笔记\n旧的一句解释。\n")
    cfg = make_cfg(tmp_path, skills_root)
    stage_pending(cfg, "notes", "# 笔记\n新的一句解释，措辞更清楚。\n")
    text = diff_mod.render(diff_mod.collect(cfg, log=lambda *_: None))
    assert "没有新增高风险能力信号" in text
