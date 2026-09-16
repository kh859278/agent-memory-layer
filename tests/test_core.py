"""配置与检索逻辑测试（不依赖真实记忆服务：检索用假的 client 注入）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from aml import config as cfgmod  # noqa: E402
from aml.retrieval import Retriever  # noqa: E402
from aml.text import clip, keep, project_of  # noqa: E402

# ------------------------------------------------------------------- config

def test_expand_env_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("AML_TEST_DIR", str(tmp_path))
    assert cfgmod.expand("$AML_TEST_DIR/x") == str(tmp_path) + "/x"
    assert cfgmod.expand("%AML_TEST_DIR%/y") == str(tmp_path) + "/y"
    assert cfgmod.expand(str(tmp_path)) == str(tmp_path)


def test_load_derives_paths_from_home(tmp_path, monkeypatch):
    monkeypatch.delenv("AML_CONFIG", raising=False)
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    assert cfg.knowledge_dir == tmp_path / "knowledge"
    assert cfg.state_dir == tmp_path / "state"
    assert cfg.backups_dir == tmp_path / "backups"
    assert cfg.api == "http://127.0.0.1:8000"


def test_load_yaml_overrides_and_cli_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("AML_CONFIG", str(tmp_path / "config.yaml"))
    (tmp_path / "config.yaml").write_text(
        "aml_home: " + str(tmp_path / "home") + "\nmemory_api: http://127.0.0.1:9000\n",
        encoding="utf-8")
    cfg = cfgmod.load()
    assert cfg.api == "http://127.0.0.1:9000"
    cfg2 = cfgmod.load({"memory_api": "http://127.0.0.1:9999"})
    assert cfg2.api == "http://127.0.0.1:9999"          # 命令行 > 配置文件


def test_init_home_creates_skeleton(tmp_path, monkeypatch):
    monkeypatch.setenv("AML_CONFIG", str(tmp_path / "home" / "config.yaml"))
    cfg = cfgmod.load({"aml_home": str(tmp_path / "home")})
    created = cfgmod.init_home(cfg)
    assert (tmp_path / "home" / "knowledge" / "沉淀").is_dir()
    assert os.path.isfile(created["config"])


# --------------------------------------------------------------------- text

def test_keep_filters_ui_noise_and_acknowledgements():
    assert keep("这是一段正常的用户请求内容")
    assert not keep("好的")
    assert not keep("OK")
    assert not keep("Insufficient Balance")
    assert not keep("短")
    assert project_of(r"C:\work\my-proj") == "my-proj"
    assert project_of(None) == "unknown"
    assert clip("a" * 400, 300).endswith("…")


# ---------------------------------------------------------------- retrieval

class FakeClient:
    """可控的假记忆服务。"""

    def __init__(self, hits):
        self.hits = hits          # [(score, memory)]
        self.calls = 0

    def search(self, query, n_results=10):
        self.calls += 1
        return list(self.hits)

    def search_by_tag(self, tags, match_all=False, n=20):
        return [m for _, m in self.hits]

    def health(self):
        return {"status": "healthy"}


def memory(content, tags=None, score_iso="2026-08-01T00:00:00Z", meta=None):
    return {"content": content, "tags": tags or [], "created_at_iso": score_iso,
            "content_hash": content[:12], "metadata": meta or {}}


def make_cfg(tmp_path, **overrides):
    data = {"aml_home": str(tmp_path)}
    data.update(overrides)
    return cfgmod.load(data)


def test_cascade_uses_loosest_tier_with_hits(tmp_path):
    cfg = make_cfg(tmp_path)
    hits = [(0.75, memory("中等相关的一条经验", ["kind:knowledge", "domain:tooling"]))]
    r = Retriever(cfg, client=FakeClient(hits)).search("随便问问", phase="P2")
    assert r.diag["tier_used"] == 0.72          # 0.80 没命中，退到 0.72（0.75 ≥ 0.72）
    assert len(r.lines) == 1
    assert "[沉淀|tooling|2026-08-01|0.75]" in r.lines[0]


def test_cascade_falls_all_the_way_to_loosest_tier(tmp_path):
    cfg = make_cfg(tmp_path)
    hits = [(0.66, memory("勉强相关", ["kind:knowledge"]))]
    r = Retriever(cfg, client=FakeClient(hits)).search("q", phase="P2")
    assert r.diag["tier_used"] == 0.65          # 0.66 只够最松那一档
    assert len(r.lines) == 1


def test_relative_margin_drops_low_band(tmp_path):
    cfg = make_cfg(tmp_path)
    hits = [(0.90, memory("很相关", ["kind:knowledge"])), (0.50, memory("不相关", ["kind:knowledge"]))]
    r = Retriever(cfg, client=FakeClient(hits)).search("q", phase="P1")
    assert len(r.lines) == 1                     # 0.50 与最高分差 > 0.07 且低于阈值，被丢掉


def test_phase_budget_is_respected(tmp_path):
    cfg = make_cfg(tmp_path)
    hits = [(0.9, memory("内容" * 200, ["kind:knowledge"]))]
    r = Retriever(cfg, client=FakeClient(hits)).search("q", phase="P2")
    assert r.budget == cfg.phase("P2")["chars"] == 600
    assert r.used <= 600


def test_empty_result_explains_why(tmp_path):
    cfg = make_cfg(tmp_path)
    r = Retriever(cfg, client=FakeClient([])).search("查不到的东西", phase="P3")
    assert r.empty
    text = r.render()
    assert "语义检索返回 0 条" in text
    assert "可换招" in text


def test_service_down_is_distinguished_from_empty(tmp_path):
    class Down:
        def search(self, query, n_results=10):
            from aml.http import MemoryAPIError
            raise MemoryAPIError("服务不可达")

    cfg = make_cfg(tmp_path)
    r = Retriever(cfg, client=Down()).search("q", phase="P0")
    assert r.diag.get("service_down") is True
    assert "不是没有记录，是查不了" in r.render()


def test_cooldown_prevents_duplicate_query(tmp_path):
    cfg = make_cfg(tmp_path)
    client = FakeClient([(0.9, memory("一条知识", ["kind:knowledge"]))])
    retriever = Retriever(cfg, client=client)
    first = retriever.search("同一个查询", phase="P2")
    second = retriever.search("同一个查询", phase="P2")
    assert first.lines and second.empty
    assert second.diag.get("cooldown_skipped") is True
    assert client.calls == 1
