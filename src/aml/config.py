"""配置解析：命令行 > 环境变量 > config.yaml > 默认值。

设计原则：
  · 程序里**不出现任何绝对路径**；所有位置都由 `aml_home` 推导或显式配置。
  · 内容与程序分离：AML_HOME 指向数据（知识库/状态/备份），仓库里只有程序。
  · 单机多项目可用：`AML_HOME` 换一个值就是另一套数据，互不干扰。
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - pipx 安装会带上 PyYAML
    yaml = None

ENV_HOME = "AML_HOME"
ENV_CONFIG = "AML_CONFIG"

DEFAULTS: dict = {
    # 数据主目录（含 knowledge/ state/ backups/）；默认 ~/aml
    "aml_home": "~/aml",
    # 下面三项留空时按 aml_home 推导
    "knowledge_dir": None,
    "state_dir": None,
    "backups_dir": None,
    # 记忆服务（mcp-memory-service 的 HTTP 端口）
    "memory_api": "http://127.0.0.1:8000",
    # 可选：SQLite 库路径（只有做"创建时间回填"这类直连操作时才需要）
    "db_path": "",
    "adapters": {
        "dsh": {
            "enabled": True,
            "sessions_dir": "~/.dsh/sessions",
            "archive_registry": "~/.dsh/storages/workspace.json",
        },
        "claude_code": {
            "enabled": True,
            "projects_dir": "~/.claude/projects",
        },
        "kimi": {
            "enabled": True,
            "sessions_dir": "%APPDATA%/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/sessions",
        },
    },
    "ingest": {
        "max_len": 300,
        "min_len": 8,
        # 路径里含这些片段的会话文件跳过。**别把 ".dsh" 放进来**：
        # DSH 自己的会话就在 ~/.dsh/sessions 下，加了会把整个 DSH 适配器过滤成 0 个文件
        # （2026-09-16 抽取时踩过）。
        "skip_path_parts": ["node_modules"],
    },
    "retrieval": {
        # 级联回退：语义@高阈值 → 语义@中 → 语义@低 → 关键词 → 兜底
        "cascade": [0.80, 0.72, 0.65],
        "rel_margin": 0.07,
        "cooldown_min": 10,
        "result_chars": 200,
        "phases": {
            "P0": {"results": 6, "chars": 1200, "intent": "定位：属于哪个项目/领域，以前做过类似的吗"},
            "P1": {"results": 5, "chars": 1500, "intent": "定方案：现成方法论、坑、决策先例"},
            "P2": {"results": 3, "chars": 600, "intent": "执行前：这个具体操作的正确姿势与陷阱"},
            "P3": {"results": 5, "chars": 1000, "intent": "卡住：这个错以前谁踩过"},
            "P4": {"results": 4, "chars": 1000, "intent": "决策：以前怎么定的、为什么、后果"},
            "P5": {"results": 5, "chars": 1000, "intent": "自检：漏了什么、验收标准是什么"},
            "P6": {"results": 3, "chars": 800, "intent": "写回：这次有什么值得留下的"},
        },
    },
    "distill": {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "max_input_chars": 8000,
        "max_out_tokens": 6000,
        "retry_model": "deepseek-v4-pro",
        "min_chars_for_retry": 3000,
        "sink_subdir": "沉淀",
    },
    "patrol": {
        "enabled": True,
        # 结尾播报的字数上限（agent 每次回答结尾念一句，不超过这个长度）
        "notify_max_chars": 100,
        # 哪些目录是"活的技能目录"，镜像到知识库的哪里（不存在就自动跳过）
        "skill_roots": [
            {"name": "agents-skills", "path": "~/.agents/skills", "mirror_to": "技能原始/skills-shared"},
            {"name": "claude-code-skills", "path": "~/.claude/skills",
             "mirror_to": "技能原始/claude-code-skills"},
        ],
        # 知识库里只读的历史快照（参与清单生成，不镜像）
        "snapshot_dirs": ["openclaw-skills", "skills-wsl", "huashu-skills"],
        # 兜底找上游时用的候选仓库（目录名命中 >= min_repo_hits 个才认）
        "candidate_repos": ["mattpocock/skills"],
        "min_repo_hits": 3,
        "inventory_relpath": "技能/技能清单.md",
        # 要监控版本的 npm 包（默认空：这是给"你自己在用的工具"留的钩子）
        "packages": [],
        "update": {"keep_backups": 10, "tarball_max_mb": 40},
    },
}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|%([A-Za-z_][A-Za-z0-9_]*)%|\$([A-Za-z_][A-Za-z0-9_]*)")


def expand(value):
    """展开 ~ / $VAR / ${VAR} / %VAR%。非字符串原样返回。"""
    if not isinstance(value, str):
        return value

    def sub(m):
        name = m.group(1) or m.group(2) or m.group(3)
        return os.environ.get(name, "")

    return os.path.expanduser(_VAR.sub(sub, value))


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def config_path(explicit: str | None = None) -> Path | None:
    """找配置文件：显式 > $AML_CONFIG > $AML_HOME/config.yaml > ./config.yaml。"""
    for cand in (explicit, os.environ.get(ENV_CONFIG)):
        if cand:
            p = Path(expand(cand))
            if p.is_file():
                return p
    home = os.environ.get(ENV_HOME)
    if home:
        p = Path(expand(home)) / "config.yaml"
        if p.is_file():
            return p
    p = Path.cwd() / "config.yaml"
    return p if p.is_file() else None


class Config:
    """薄封装：cfg["memory_api"] / cfg.path("knowledge_dir") / cfg.phase("P2")。"""

    def __init__(self, data: dict, source: Path | None = None):
        self.data = data
        self.source = source

    # ---- 读取 ----
    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def section(self, name: str) -> dict:
        return self.data.get(name) or {}

    def path(self, key: str) -> Path:
        return Path(expand(self.data[key]))

    @property
    def home(self) -> Path:
        return Path(expand(self.data["aml_home"]))

    @property
    def knowledge_dir(self) -> Path:
        return Path(expand(self.data["knowledge_dir"]))

    @property
    def state_dir(self) -> Path:
        return Path(expand(self.data["state_dir"]))

    @property
    def backups_dir(self) -> Path:
        return Path(expand(self.data["backups_dir"]))

    @property
    def api(self) -> str:
        return str(self.data["memory_api"]).rstrip("/")

    def phase(self, name: str) -> dict:
        phases = self.section("retrieval").get("phases") or {}
        return phases.get((name or "P0").upper()) or phases.get("P0") or {}

    def adapter(self, name: str) -> dict:
        return (self.section("adapters") or {}).get(name) or {}

    # ---- 写入 ----
    def save(self, path: Path | None = None) -> Path:
        target = Path(path) if path else (config_path() or (self.home / "config.yaml"))
        if yaml is None:  # pragma: no cover
            raise RuntimeError("需要 PyYAML 才能写配置文件：pip install PyYAML")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.data, f, allow_unicode=True, sort_keys=False)
        return target


def load(cli_overrides: dict | None = None) -> Config:
    """加载配置并补齐推导路径。

    cli_overrides 只放"用户显式指定的顶层键"（如 aml_home / knowledge_dir）。
    """
    data = copy.deepcopy(DEFAULTS)
    cfg_file = config_path()
    if cfg_file:
        if yaml is None:  # pragma: no cover
            raise RuntimeError("发现配置文件但缺少 PyYAML：pip install PyYAML")
        with open(cfg_file, encoding="utf-8") as f:
            data = deep_merge(data, yaml.safe_load(f) or {})
    env_home = os.environ.get(ENV_HOME)
    if env_home:
        data["aml_home"] = env_home
    if cli_overrides:
        data = deep_merge(data, {k: v for k, v in cli_overrides.items() if v is not None})

    home = Path(expand(data["aml_home"]))
    for key, sub in (("knowledge_dir", "knowledge"), ("state_dir", "state"), ("backups_dir", "backups")):
        if not data.get(key):
            data[key] = str(home / sub)
    return Config(data, cfg_file)


def init_home(cfg: Config, force: bool = False) -> dict:
    """创建数据目录骨架 + 写一份 config.yaml。返回各路径。"""
    created = {}
    for key, p in (("aml_home", cfg.home), ("knowledge_dir", cfg.knowledge_dir),
                   ("state_dir", cfg.state_dir), ("backups_dir", cfg.backups_dir)):
        p.mkdir(parents=True, exist_ok=True)
        created[key] = str(p)
    (cfg.knowledge_dir / "沉淀").mkdir(exist_ok=True)
    (cfg.knowledge_dir / "源知识").mkdir(exist_ok=True)
    target = Path(os.environ.get(ENV_CONFIG) or (cfg.home / "config.yaml"))
    if force or not target.exists():
        data = dict(cfg.data)
        data["aml_home"] = str(cfg.home)
        Config(data).save(target)
        created["config"] = str(target)
    return created
