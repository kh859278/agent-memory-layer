# agent-memory-layer —— 仓库内的统一管理约定

> 这份文件由**主控会话**（用户在 DSH 里指定的那一个）维护。
> DSH 会按目录层级自动加载 AGENTS.md，所以任何在本目录里动手的会话都会读到它。

## 一、为什么需要它

这台机器上**同时有多个 agent 会话**在改这个仓库（2026-09-16 实测：一边在做技能治理移植，
一边在做 MCP server / dedup）。两边共享同一个工作目录和同一个 `.git`，已经出过一次事故：
一个会话用 `git add -A` 提交，把另一个会话刚生成、还不打算提交的开发探针脚本一起扫进了仓库。

## 二、硬规则（违反会导致别人的工作被误提交或丢失）

| 规则 | 说明 |
|---|---|
| **1. 提交与推送统一走守门器** | `python tools/repo_guard.py --message-file <msg.txt> --paths <路径...>`。它加锁串行化、对表远端、拒绝 `add -A`、跑测试/lint/泄漏扫描、然后提交推送 |
| **2. 永远不要 `git add -A` / `git add .`** | 按路径暂存。守门器要求显式列出本次要提交的文件，并且**暂存区必须先是空的** |
| **3. 提交前先 `git status` 看清归属** | 不是自己改的文件**不要**提交、不要还原、不要格式化 |
| **4. 发现别人的未提交改动 → 写交接单** | `python tools/repo_guard.py --handoff "改了什么" --paths a.py b.py`；主控会话提交前会读 `state/handoff/` |
| **5. 远端有新提交 → 先 `git pull --rebase`** | 守门器会拒绝"落后/分叉"状态下的提交，别强推 |
| **6. 只有主控会话提交** | 其他会话：改完文件 + 写交接单，把提交留给主控；需要立刻提交时先和主控打招呼 |

## 三、状态与配置的唯一来源（别再各自造一套）

| 东西 | 唯一位置 | 备注 |
|---|---|---|
| 配置 | `agent-memory-layer/config.local.yaml` | 已设为用户级环境变量 `AML_CONFIG`；新进程读不到时显式 `$env:AML_CONFIG=<该路径>` |
| 数据 | `$AML_HOME`（默认 `~/aml`，本机指向 `Desktop\整理`） | `knowledge/`（可读镜像）、`state/`（运行状态）、`backups/`（备份） |
| 巡检引擎 | `aml patrol ...`（本仓库） | 旧的 `记忆层\skills-watch\*.py` 保留为 `-Legacy` 回滚路径，**不再往里加功能** |
| Windows 外壳 | `记忆层\skills-watch\skills-watch.ps1` + 隐藏 VBS + 计划任务 | 只负责隐藏窗口/编码/日志，里面只有一行干活：调 `aml patrol run` |
| 通知播报 | `aml patrol notify --brief` / `--ack` | agent 在回答结尾念 ≤100 字 |

**改动落点原则**：功能改在本仓库（有测试、配置驱动、跨平台）；Windows 专属的坑留在外壳里，
并在注释里写清为什么不能搬（ConPTY 弹窗、PS 5.1 按 GBK 读脚本、wscript 吞非 ASCII 字节、
GBK 解码子进程 stdout）。

## 四、提交前的最小验证（守门器会自动跑，手动改完也可以先自查）

```bash
pytest -q                       # 76+ 个测试；改代码前请再用 3.9 跑一遍（见 README 开发章节）
ruff check src tests tools
python tools/scrub_check.py     # 内容泄漏扫描：绝对路径/凭据/邮箱/屏蔽词
python tools/repo_guard.py --status
```

## 五、回滚点

| 场景 | 怎么回滚 |
|---|---|
| 技能被自动更新覆盖 | `state/patrol/_backup/<技能名>-<旧sha>-<时间戳>/` 拷回技能目录 |
| 采纳了不想采纳的上游版本 | `aml patrol accept` 之前会自动备份，同样从 `_backup/` 恢复 |
| 知识去重合并错了 | `aml dedup --rollback state/backups/dedup/dedup-YYYYmmdd-HHMMSS.json` |
| 记忆库损坏 | `aml restore`（先预检 integrity_check，再自动另存现有库） |
| 巡检整体退回旧实现 | `powershell -File 记忆层\skills-watch\skills-watch.ps1 -Legacy` |
