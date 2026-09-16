# agent-memory-layer (aml)

**把你的 coding agent 历史变成可复用的资产** —— 跨 agent 采集会话、保留原始时间轴、
自动蒸馏成跨项目知识、按阶段检索、并同步一份人能读的 markdown。

> ⚠️ **状态：早期抽取中（Step 1/2）**。本仓库从一套已在本机跑了几周的自建系统里抽取，
> 代码还在从"能跑"变成"能装"的路上。见 `ROADMAP.md`。

## 它解决什么问题

你同时用 DSH / Claude Code / Codex / Kimi，每个 agent 的历史都锁在自己的目录里，
换个 agent 就"不记得"上次怎么解决的；而且这些历史只是流水账，第二次遇到同样的问题
还得重新踩坑。

`aml` 做四件事：

1. **采集**：从各 agent 的会话文件里挖对话（一条"任务"一条"回复"），
   **保留原始时间戳**（不是写入时间），所以能按历史时间检索。
2. **蒸馏**：会话结束/归档时，用 LLM 把它提炼成**跨项目可复用的知识**
   （`kind:knowledge` + `domain:<领域>`），与只对当前项目成立的流水账分开存放。
3. **检索**：**分阶段检索协议 P0–P6**（定位/定方案/执行前/卡住/决策/自检/写回），
   每阶段有固定条数与字数预算、层级优先（跨项目沉淀 > 本项目历史 > 全库）、同查询去重，
   未命中时必须解释"为什么空"（避免把"没查到"误判成"没有历史约定"）。
4. **人面镜像**：向量库里的知识同步落成 `knowledge/沉淀/<领域>.md`，
   加上自动重建的索引，所以**没有这个程序也能读**，数据不被锁在数据库里。

## 快速开始（目标形态）

```bash
pipx install agent-memory-layer
aml init                      # 生成 $AML_HOME/config.yaml，检测本机有哪些 agent
aml doctor                    # 一条命令体检：服务、索引新鲜度、embedding 覆盖、阈值自测
aml sync                      # 采集增量入库（首次想从零开始：先 aml watch --seed 记基线）
aml search --phase P2 "powershell 编码"
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `aml init` | 建数据目录骨架 + `config.yaml`（并探测本机能看到的 agent 会话） |
| `aml doctor` | 体检：目录 / 服务 / 向量覆盖 / FTS / **索引新鲜度** / 适配器 / 检索自测，每项带修法 |
| `aml sync` | 采集所有 agent 会话入库 + **把 created_at 回填成原始时间**（`--dry-run` 只看） |
| `aml watch` | 常驻监听：DSH 归档即时入库、其他 agent 会话静默 ≥120s 后增量入库（`--seed` 记基线） |
| `aml distill` | 把会话蒸馏成跨项目知识（`kind:knowledge` + `domain:*`），并落可读 markdown |
| `aml search QUERY --phase P2` | 分阶段检索：级联回退 + 未命中解释（`--explain` 看诊断） |
| `aml ingest-kb` | 知识库文档分块灌库（`--since` 增量） |
| `aml index rebuild` | 重建知识库索引（索引会腐化，体检会告警） |
| `aml backup` / `aml restore` | 在线备份（按天保留）/ **从备份恢复**（默认预检，`--yes` 才写，且先自动备份现有库） |
| `aml export OUT` | 导出成人可读 markdown（按领域分组）或原始 JSON |
| `aml review` | 知识复核：列出过期/快到期的结论，`--postpone <hash> --days 180` 顺延 |
| `aml denoise` | 找出界面回显/纯确认语等噪声，`--apply` **软删除**（写 `deleted_at`，可回滚） |

底层记忆服务用 [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service)
（HTTP + MCP，SQLite + sqlite-vec + 本地嵌入），任何 MCP 客户端都能接。

## 隐私

**采集、嵌入、检索全在本机**；只有"蒸馏"这一步会调一次你指定的 LLM API（可关）。
本仓库**不含任何用户内容**：状态、日志、知识库正文、会话原文都不入库，
CI 里有一个内容泄漏扫描（`tools/scrub_check.py`）当守门。

## 目录

```
src/aml/
├─ config.py        配置解析（AML_HOME / config.yaml / 环境变量 / 命令行）
├─ http.py          记忆服务客户端（重试、超时、批量）
├─ text.py          截断 / 降噪判定 / 项目名归一 / 时间戳归一
├─ adapters/        agent 会话格式适配器：dsh / claude_code / kimi
├─ ingest.py        会话 → 记忆层（去重、降噪、时间戳回填）
├─ watch.py         常驻监听：归档触发 / 文件静默触发
├─ kb.py            知识库文档入库 + 索引重建
├─ retrieval.py     分阶段检索（级联回退 + 诊断 + 预算）
├─ distill.py       会话 → 跨项目知识（LLM 提炼 + 可读 markdown 落盘）
├─ maintenance.py   备份 / 恢复 / 导出 / 复核 / 降噪
├─ doctor.py        体检
└─ cli.py           命令行入口
```

> 技能治理（上游版本监控 + 安全更新 + 自动入库）在原系统里已跑通，尚未搬进本仓库，见 `ROADMAP.md`。

## 许可证

MIT，见 `LICENSE`。
