# 发布与版本（RELEASING）

## 分发名：`aml-memory`（2026-09-21 定，2026-09-22 首发）

- **已发布 `aml-memory 0.1.0`**：https://pypi.org/project/aml-memory/ （wheel + sdist），
  安装即为 `pipx install aml-memory` / `uv tool install aml-memory`（命令仍是 `aml`）
- **GitHub 仓库名不变**（`agent-memory-layer`）——PyPI 上那个 `agent-memory-layer` 是**另一个
  不相关的项目**（SAP 的 "A reusable memory layer for SAP agentic workflows"，0.1.0/0.1.1，2026-04 上传）
- `aml self-update` 会先查 PyPI 并**校验归属**（`OWNER_MARKERS`）：包里声明的仓库链接不是我们的就拒绝升级
- 改名已落到这些地方（2026-09-21）：`pyproject.toml` 的 `name`、`src/aml/selfupdate.py` 的
  `PACKAGE`、`aml --version` 文案、`tests/test_selfupdate.py` 的断言、README/本文件/PROMOTION/ROADMAP
- ⚠️ 还有一处**没改**：`src/aml/patrol/lockfile.py` 的 generator 字符串（`agent-memory-layer/<版本>`）
  是另一个会话当时未提交的文件（现已代提交），已写交接单（见 `state/handoff/`）

### 发布凭据（2026-09-22 起改用免密钥方式）

**首选：PyPI Trusted Publishing（OIDC，不需要任何长期 token）**

1. PyPI → 项目 `aml-memory` → Publishing → 添加 Trusted Publisher：
   Owner `kh859278`、Repository `agent-memory-layer`、Workflow `release.yml`、Environment `pypi`
2. GitHub → Settings → Environments → 建 `pypi`（**建议加 required reviewers**，
   等于"发布要人点一下"）
3. 跑 `.github/workflows/release.yml`（先手动 `workflow_dispatch`；配好之后再打开 tag 自动发布）

**为什么要换**：token 是长期凭据，泄露等于任何人都能往 PyPI 推一版"我们的"包；
而发布这件事发生在一台**同时跑多个能改仓库的 agent** 的机器上 —— 长期凭据放那里风险不对称。
OIDC 拿到的是一次性、几分钟就失效的凭据，且不落盘。

**回退：API token（只在暂时没配 OIDC 时用）**

- `uv publish --username __token__ --password pypi-…`，或 `twine upload -u __token__ -p pypi-… dist/*`；
  上传本身不需要每次输 2FA 码（但账号必须启用 2FA，否则 PyPI 拒绝上传）
- **token 只在发布时用一次**：发完就删（Account settings → API tokens → Remove），
  下次发版再建新的
- token 里 `pypi-` 后面是 base64：**长度必须是 4 的倍数或 4n+2、4n+3**；
  长度 mod 4 == 1 说明复制时丢了字符（本机实测过一次：`uv publish` 报
  `403 Invalid or non-existent authentication information`，就是这个问题）

## 发一版（名字定了之后）

```bash
# 1) 版本与元数据
#    改 pyproject.toml 的 version、CITATION.cff 的 version/date-released
python -m pytest -q                      # 本地必须全绿
ruff check src tests tools
python tools/scrub_check.py              # 内容泄漏扫描：绝不能把本机路径/客户名/密钥带上

# 2) 打 tag 与 GitHub Release（Release notes 用下面的模板）
git tag -a v0.1.0 -m "v0.1.0" && git push origin v0.1.0

# 3) 发布到 PyPI
#    推荐：跑 `.github/workflows/release.yml`（OIDC，无 token，见上）
#    回退：本地手工（需要 PYPI_TOKEN）
uv build && uv publish --token "$PYPI_TOKEN"
# 或：python -m build && twine upload dist/* -u __token__ -p "$PYPI_TOKEN"
```

发布后**回改**：README 的安装段、`install.ps1`/`install.sh` 的提示、`ROADMAP.md` 的发布 checklist、
`src/aml/selfupdate.py` 的 `PACKAGE` 常量（改它属于另一个会话正在动的文件，**先发交接单**）。

## Release notes 模板

```markdown
## 新增
- <按"用户能感知到的能力"写，不按包名/文件名列>

## 行为变化
- <默认值、阈值、命名、输出格式的变化；每条都要写"旧 → 新">

## 修复
- <错在哪、为什么错、怎么验证的>

## 数据（诚实版）
- 测试：<n> 个通过（3.9 / 3.12）
- 基准：检索层 <命中率 / P@3 / 注入量分位>；任务层 <成功率差值 / 成本>
  —— 若某次基准结果不利于我们，也要写出来（例：3 任务 × 2 组，成功率差值 +0%）
```

## 版本号怎么走

`0.x` 阶段：**接口还会变**，所以
- 破坏兼容 → `0.(x+1).0`，并在 Release notes 里写清"怎么迁移"
- 只加能力 → `0.x.(y+1)`

## 回滚

- 代码：`git revert <sha>`（本仓库提交都走 `tools/repo_guard.py`，历史线性）
- 技能被覆盖：`state/patrol/_backup/<技能名>-<旧sha>-<时间戳>/` 拷回
- 记忆库损坏：`aml restore`（先预检 integrity_check，再自动另存现有库）
- 运行数据被删：`backups/state/<时间戳>/` 拷回（巡检每天自动打一份）
