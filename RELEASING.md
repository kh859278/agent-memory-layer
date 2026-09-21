# 发布与版本（RELEASING）

## 现在卡在哪：PyPI 分发名被占用

`agent-memory-layer` 这个 **PyPI 分发名已经属于另一个不相关的项目**
（SAP 的 `agent-memory-layer`，v0.1.1，2026-04-18 上传，摘要 "A reusable memory layer for SAP
agentic workflows"）。所以：

- README/安装脚本里的安装方式一律是 **`git+https://…`**（源码装），不要写成 `pipx install agent-memory-layer`
- `aml self-update` 会先查 PyPI 并**校验归属**：不是我们的包就拒绝升级，退查 git tag
- 发行名要在下面几个里挑一个（2026-09-21 实测都还空着）：
  `aml-memory`（推荐，短、和 CLI 名一致）／`aml-memory-layer`／`aml-cli`／`agent-memory-layer-cli`
  **决定之前不要改 `pyproject.toml` 的 `name`**（那个字段一改就是另一个包的身份）

## 发一版（名字定了之后）

```bash
# 1) 版本与元数据
#    改 pyproject.toml 的 version、CITATION.cff 的 version/date-released
python -m pytest -q                      # 本地必须全绿
ruff check src tests tools
python tools/scrub_check.py              # 内容泄漏扫描：绝不能把本机路径/客户名/密钥带上

# 2) 打 tag 与 GitHub Release（Release notes 用下面的模板）
git tag -a v0.1.0 -m "v0.1.0" && git push origin v0.1.0

# 3) 发布到 PyPI（二选一；需要 PYPI_TOKEN）
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
