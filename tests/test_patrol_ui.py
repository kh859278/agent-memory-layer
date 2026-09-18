"""只读视图测试：全部离线，只碰临时目录。

要钉死的口径：
  · 汇总取数**每一块都容错**：某块抛异常时页面照常生成，别的块不受影响
  · HTML 转义：含 `<script>` 的**技能名**（技能名来自上游仓库，按威胁模型就是要防的那类输入）
  · **页面上不允许出现任何 http/https 外链**（自包含页面，不打开就发请求）
  · 报告区**只 stat 不读内容**（用"读不出来"的文件证明它没被读）
  · bench 只取汇总字段（`tasks/runs/arms/delta`），`rows` 之类的一律不进页面
  · `write` 落到 `state/patrol/ui/index.html`，UTF-8 + LF
"""
from __future__ import annotations

import json
import os

from aml import config as cfgmod
from aml.patrol import capability, scopes, sources, ui


def make_skill(root, name, body="正文", extra=None):
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
        # front-matter 的 name 用目录名（与真实技能一致）；`name` 里可能带尖括号
        f.write(f"---\nname: {name}\ndescription: 测试技能\n---\n\n{body}\n")
    for rel, content in (extra or {}).items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    return path


def skill_dir(tmp_path, dir_name, front_matter_name):
    """技能**目录名**安全、front-matter 里的 name 是脏的。

    必须分开：Windows 的目录名不允许 `<` / `>`（`WinError 123`），
    但真实世界里"脏名字"进页面的路径恰恰是 front-matter / 上游仓库给的字符串。
    """
    path = os.path.join(str(tmp_path / "live"), dir_name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {front_matter_name}\ndescription: 测试技能\n---\n\n正文\n")
    return path


def make_cfg(tmp_path, roots=None, scopes_cfg=None, **patrol):
    cfg = cfgmod.load({"aml_home": str(tmp_path)})
    cfg.data["patrol"]["skill_roots"] = roots if roots is not None else []
    cfg.data["patrol"]["snapshot_dirs"] = []
    if scopes_cfg is not None:
        cfg.data["patrol"]["scopes"] = scopes_cfg
    if patrol:
        cfg.data["patrol"].update(patrol)
    return cfg


def test_build_collects_the_six_blocks(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "s1")
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "技能原始/old"}])
    data = ui.build(cfg)
    assert data["scopes"][0]["name"] == "old" and data["scopes"][0]["skills"] == 1
    assert data["lifecycle"][0]["name"] == "s1"
    assert data["lifecycle_counts"]["discovered"] == 1
    assert data["reports"] == []          # 还没有报告目录
    assert data["bench"] is None          # 还没有 bench 报告
    assert data["notify"]                 # 队列为空时也有"（队列为空）"这种纯文本
    assert data["errors"] == []


def test_build_sources_only_keeps_public_fields(tmp_path):
    cfg = make_cfg(tmp_path, [], candidate_repos=["owner/repo"])
    row = ui.build(cfg)["sources"][0]
    assert row["repo"] == "owner/repo"
    assert set(row) == set(ui.SOURCE_FIELDS)
    assert "added_at" in row and "mirror_to" not in row


def test_build_survives_broken_blocks(tmp_path, monkeypatch):
    """单块炸掉不许带崩整页：错误进 `errors`，其余块照常。"""
    cfg = make_cfg(tmp_path, [])

    def boom(*_a, **_kw):
        raise RuntimeError("炸了")

    monkeypatch.setattr(scopes, "summary", boom)
    monkeypatch.setattr(capability, "overview", boom)
    monkeypatch.setattr(sources, "load", boom)
    data = ui.build(cfg)
    assert data["scopes"] == [] and data["lifecycle"] == [] and data["sources"] == []
    assert len(data["errors"]) == 3
    page = ui.render_html(data)          # 照常出页面
    assert "技能与记忆层状态（只读）" in page


def test_build_reports_only_stat_and_newest_five(tmp_path):
    reports = tmp_path / "state" / "patrol" / "reports"
    reports.mkdir(parents=True)
    # 故意写 6 份"内容不是 JSON"的报告：如果实现去读内容，这里就会变成 error 而不是文件名列表
    for i in range(6):
        (reports / f"run-{i}.json").write_text("这不是 JSON{", encoding="utf-8")
    cfg = make_cfg(tmp_path, [])
    rows = ui.build(cfg)["reports"]
    assert len(rows) == ui.REPORTS_LIMIT == 5
    assert all(row["name"].startswith("run-") for row in rows)
    assert all(row["mtime"] and row["bytes"] > 0 for row in rows)


def test_bench_keeps_only_summary_fields(tmp_path):
    runs = tmp_path / "state" / "bench" / "task-runs"
    runs.mkdir(parents=True)
    report = {"tasks": 7, "runs": 14, "arms": {"on": {"runs": 7}, "off": {"runs": 7}},
              "delta": 0.5, "rows": [{"id": "t1", "score": 1}], "memory_items": ["秘密正文"]}
    (runs / "task-bench-20260918-010101.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8")
    bench = ui.build(make_cfg(tmp_path, []))["bench"]
    assert bench["file"] == "task-bench-20260918-010101.json"
    assert bench["tasks"] == 7 and bench["runs"] == 14 and bench["delta"] == 0.5
    assert set(bench) == {"file", "mtime", "tasks", "runs", "arms", "delta"}
    page = ui.render_html(ui.build(make_cfg(tmp_path, [])))
    assert "秘密正文" not in page and "score" not in page


def test_bench_survives_broken_json(tmp_path):
    runs = tmp_path / "state" / "bench" / "task-runs"
    runs.mkdir(parents=True)
    (runs / "task-bench-broken.json").write_text("{不是 json", encoding="utf-8")
    bench = ui.build(make_cfg(tmp_path, []))["bench"]
    assert bench["file"] == "task-bench-broken.json" and bench["error"]


# ------------------------------------------------------------------ 转义与安全

def test_html_escapes_script_in_skill_name(tmp_path):
    """技能名来自上游仓库 → 必须当成不可信输入；未转义就是一个 XSS。

    这里走的是"数据进渲染器"这条路（技能名的来源按实现是**目录名**，
    而 Windows 目录名不允许 `<>`，所以用脏 front-matter name 直接喂 `render_html`）。
    """
    live = tmp_path / "live"
    make_skill(str(live), "safe-dir")
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "m"}])
    data = ui.build(cfg)
    data["lifecycle"] = [{"name": "<script>alert(1)</script>", "state": "active",
                          "declared": [], "undeclared": []}]
    data["lifecycle_counts"] = {"active": 1}
    page = ui.render_html(data)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_html_escapes_dirty_name_from_front_matter(tmp_path):
    """端到端那条路也钉一下：front-matter 里的脏 name 出现在报告区时同样要转义。"""
    live = tmp_path / "live"
    skill_dir(tmp_path, "safe-dir", '"><script>alert(2)</script>')
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "m"}])
    page = ui.render_html(ui.build(cfg))
    assert "<script>alert(2)</script>" not in page


def test_html_escapes_paths_and_repo_names(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "s1")
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "m"}],
                   candidate_repos=['"><img src=x onerror=alert(1)>'])
    page = ui.render_html(ui.build(cfg))
    assert "<img src=x" not in page
    assert "&quot;&gt;&lt;img" in page


def test_html_has_no_external_links(tmp_path):
    """连"去 PyPI 看新版"这种链接都不放过：页面必须自包含、打开时不发请求。"""
    live = tmp_path / "live"
    make_skill(str(live), "s1", body="正文里有个链接 https://example.com/secret")
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "m"}])
    page = ui.render_html(ui.build(cfg))
    assert "http://" not in page and "https://" not in page
    assert "//example.com" not in page


def test_html_does_not_include_skill_body(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "s1", body="这是技能正文，不该出现在页面里")
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "m"}])
    page = ui.render_html(ui.build(cfg))
    assert "这是技能正文" not in page
    assert "s1" in page                 # 但技能名要在


def test_esc_helper():
    assert ui.esc(None) == ""
    assert ui.esc(True) == "是" and ui.esc(False) == "否"
    assert ui.esc("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"
    assert ui.esc('a"b') == "a&quot;b"


# ------------------------------------------------------------------ 渲染与落盘

def test_render_html_has_title_and_structure(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "s1")
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "m"}])
    page = ui.render_html(ui.build(cfg))
    assert page.startswith("<!DOCTYPE html>")
    assert "<title>技能与记忆层状态（只读）</title>" in page
    assert "<style>" in page and "<script" not in page.lower()
    assert 'charset="utf-8"' in page      # 缺了它中文 Windows 上是乱码
    for section in ("作用域与技能目录", "来源", "技能生命周期", "待批", "最近", "bench", "通知队列"):
        assert section in page
    assert "只读" in page


def test_render_html_handles_empty_cfg(tmp_path):
    page = ui.render_html(ui.build(make_cfg(tmp_path, [])))
    assert "技能与记忆层状态（只读）" in page
    assert "（空）" in page or "（还没有" in page


def test_write_to_default_path_utf8_lf(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "中文技能")
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "m"}])
    out = ui.write(cfg)
    assert out == str(tmp_path / "state" / "patrol" / "ui" / "index.html")
    with open(out, "rb") as f:
        raw = f.read()
    assert b"\r\n" not in raw                      # 强制 LF
    assert "中文技能" in raw.decode("utf-8")        # UTF-8 写盘
    assert raw.decode("utf-8").startswith("<!DOCTYPE html>")


def test_write_accepts_explicit_out(tmp_path):
    cfg = make_cfg(tmp_path, [])
    target = tmp_path / "somewhere" / "deep" / "view.html"
    assert ui.write(cfg, out=target) == str(target)
    assert target.is_file()                        # 父目录会自动建


def test_lifecycle_flags_render_high_risk(tmp_path):
    live = tmp_path / "live"
    make_skill(str(live), "risky", body="```bash\ncurl https://x.example.com\n```")
    cfg = make_cfg(tmp_path, [{"name": "old", "path": str(live), "mirror_to": "m"}])
    page = ui.render_html(ui.build(cfg))
    assert "未声明高危" in page
    assert "network" in page
