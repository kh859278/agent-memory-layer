# agent-memory-layer

[![ci](https://github.com/kh859278/agent-memory-layer/actions/workflows/ci.yml/badge.svg)](https://github.com/kh859278/agent-memory-layer/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

**把你的 coding agent 历史变成可复用的资产。**
跨 agent 采集会话、保留**原始时间轴**、自动蒸馏成跨项目知识、按阶段检索，
并同步一份**人能读的 markdown**（不被锁在数据库里）。

> **状态：alpha。** Step 1（拆仓去内容）与 Step 2（可安装可验证）已完成，见 [`ROADMAP.md`](ROADMAP.md)。
> 测试与 CI 是绿的；接口还会变。**尚未发布到 PyPI**，请从源码装。

---

## 为什么需要它

你同时用 DSH / Claude Code / Codex / Kimi 这类 coding agent。每个 agent 的历史都锁在自己的目录里：
换个 agent 就"不记得"上次怎么解决的；而且这些历史只是流水账——
第二次遇到同一个坑，还得重新踩一遍。

`aml` 把"历史"变成"资产"：

| 做的事 | 具体行为 |
|---|---|
| **采集** | 从各 agent 的会话文件里挖对话（一条"任务"一条"回复"），**保留原始时间戳**而不是写入时间 |
| **蒸馏** | 会话结束/归档时用 LLM 提炼成**跨项目可复用的知识**（`kind:knowledge` + `domain:<领域>`），与"只对本项目成立"的流水账分开存 |
| **检索** | **分阶段检索协议 P0–P6**：每阶段有固定条数与字数预算、层级优先（跨项目沉淀 > 本项目历史 > 全库）、同查询去重；**未命中必须解释为什么空** |
| **人面镜像** | 沉淀同步落成 `knowledge/沉淀/<领域>.md` + 自动重建索引：没有本程序也能读、能 grep、能搬走 |
| **技能治理** | 你装的 skill 也能被纳管：比上游版本、干净就自动更新、改过就只暂存（见下） |

## 30 秒看效果

体检一条命令（输出为格式示意，真实运行时每一项都带"怎么修"）：

```console
$ aml doctor
记忆层体检：14 项，❌ 0，⚠️ 1
✅ 记忆服务：http://127.0.0.1:8000 健康
✅ 向量覆盖：9xxx/9xxx（100.0%）
✅ FTS 全文索引：9xxx 条
✅ 记忆条数：库内 9xxx（有效 9xxx），库更新于 2026-09-16 21:02
⚠️ 索引新鲜度：索引自报 8xxx，库内 9xxx，差 1xxx 条
     修复：aml index rebuild
✅ 适配器 dsh：206 个会话文件
✅ 适配器 claude_code：9 个会话文件
✅ 适配器 kimi：8 个会话文件
✅ 检索自测：泛词查询命中 1 条（tier 0.8）
```

分阶段检索（P2 = 动手前，只要 3 条 / 600 字）：

```console
$ aml search "powershell 编码 乱码" --phase P2 --explain
# 阶段检索 P2（3 条 / 600/600 字）
[沉淀|env-windows|2026-09-13|0.85] 【…】Windows PowerShell 5.1 默认按 GBK 解析脚本…
[沉淀|env-windows|2026-09-13|0.85] 【…】控制台代码页与输出编码不一致会乱码…
[沉淀|env-windows|2026-09-13|0.85] 【…】后台跑 Python 文本脚本先设 PYTHONUTF8=1…
诊断：{"candidates": 60, "top": 0.854, "tier_used": 0.8, "fts_hits": 0}
```

没命中时它不会装作"没有"，而是告诉你为什么：

```console
$ aml search "内存快照比对" --phase P3
# 阶段检索 P3（0 条 / 0/1000 字）
（没命中。下面说明为什么，避免把「没查到」当成「没有」）
· 语义返回 42 条，最高分 0.61；达到最松阈值(0.65)的有 0 条
· 可换招：① 换措辞 ② 放宽 -n ③ 用 --tag domain:xxx 收窄 ④ aml recall --grep 关键词
```

## 一键安装

**Windows（PowerShell）**

```powershell
powershell -c "irm https://raw.githubusercontent.com/kh859278/agent-memory-layer/main/install.ps1 | iex"
```

**macOS / Linux**

```sh
curl -fsSL https://raw.githubusercontent.com/kh859278/agent-memory-layer/main/install.sh | sh
```

脚本只做四件事，每步都打印在做什么：检查 Python ≥3.9 与 git → 装 `aml` 命令行
（优先 `uv`，其次 `pipx`，最后 `pip --user`）→ 可选装上本地后端 `mcp-memory-service`
→ `aml init` + `aml doctor`。**幂等**（装过就跳过）、不需要管理员、不改系统目录。

想先看一眼再跑（推荐，尤其是 `| sh` 这种形式）：

```powershell
irm https://raw.githubusercontent.com/kh859278/agent-memory-layer/main/install.ps1 -OutFile install.ps1; notepad install.ps1; .\install.ps1
```

常用开关：`-DryRun`（只看会执行什么）/ `-Upgrade` / `-NoBackend` / `-NoInit`
（`sh install.sh --dry-run --upgrade --no-backend --no-init`）。

### 只想装 CLI 本体（自己管后端）

```bash
uv tool install "git+https://github.com/kh859278/agent-memory-layer.git"     # 或
pipx install "git+https://github.com/kh859278/agent-memory-layer.git"        # 或
pip install "git+https://github.com/kh859278/agent-memory-layer.git"
```

开发模式：

```bash
git clone https://github.com/kh859278/agent-memory-layer.git && cd agent-memory-layer
pip install -e ".[dev]"
aml init                      # 建数据目录 + config.yaml，并探测本机能看到的 agent 会话
```

### 依赖一个本地记忆服务

`aml` 是**采集/蒸馏/检索/治理**这一层；向量存储与检索后端用
[mcp-memory-service](https://github.com/doobidoo/mcp-memory-service)（HTTP + MCP，
SQLite + sqlite-vec + 本地嵌入模型）。让它在 `127.0.0.1:8000` 跑着即可，任何 MCP 客户端也能接。

```yaml
# $AML_HOME/config.yaml 里最关键的两项
memory_api: http://127.0.0.1:8000
db_path: /path/to/mcp-memory/sqlite_vec.db   # 只有"时间回填"等直连操作才需要
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `aml init` | 建数据目录骨架 + `config.yaml`（并探测本机能看到的 agent 会话） |
| `aml doctor` | 体检：服务 / 向量覆盖 / FTS / **索引新鲜度** / 适配器 / 检索自测，每项带修法 |
| `aml sync` | 采集所有 agent 会话入库 + **把 created_at 回填成原始时间**（`--dry-run` 只看） |
| `aml watch` | 常驻监听：归档即时入库、会话静默 ≥120s 后增量入库（`--seed` 只记基线） |
| `aml distill` | 会话 → 跨项目知识（`--list` / `--session` / `--rebuild-md`） |
| `aml search Q --phase P2` | 分阶段检索：级联回退 + 未命中解释（`--explain` 看诊断；`--include-procedure` 才返回技能正文） |
| `aml ingest-kb` | 知识库文档分块灌库（`--since` 增量） |
| `aml index rebuild` | 重建知识库索引（索引会腐化，`doctor` 会告警） |
| `aml backup` / `aml restore` | 在线备份（按天保留）/ **从备份恢复**（默认预检，`--yes` 才写，且先自动另存现有库） |
| `aml export OUT` | 导出成人可读 markdown（按领域分组）或原始 JSON |
| `aml review` | 知识复核：列出过期/快到期的结论，`--postpone <hash> --days 180` 顺延 |
| `aml denoise` | 找出界面回显/纯确认语等噪声，`--apply` **软删除**（写 `deleted_at`，可回滚） |
| `aml dedup` | 近义知识合并（默认只预览；`--apply` 才合并，**整簇快照可 `--rollback`**） |
| `aml backfill-embeddings` | 补齐**缺向量**的记录（缺向量 = 语义检索永远搜不到），`--prune-orphans` 清重复孤儿 |
| `aml mcp` | 起 MCP server（stdio JSON-RPC），把检索/写回/播报/体检暴露给任何 MCP 客户端 |
| `aml patrol run` | 技能治理一轮：纳管 → 更新 → 镜像入库 → 包版本 → 写播报队列 |
| `aml patrol check` / `update` | 比上游 commit：**干净的自动更新，本地改过的只暂存**（`--deep` 用内容指纹兜底） |
| `aml patrol adopt` | 给没有上游来源的技能补元数据（目录名命中 ≥3 个才认仓库；默认只暂存不覆盖） |
| `aml patrol accept NAME --all` | 采纳暂存的上游版本（覆盖本地，先备份） |
| `aml patrol packages` | 包版本监控（只监控不升级，附 release notes 摘要） |
| `aml patrol notify --brief` | 取一句 ≤100 字的「这次新增/更新了什么」（没变化则空输出） |

## 架构（数据怎么流）

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ 各个 coding agent —— 历史都锁在各自目录里                                     │
│   DSH  ~/.dsh/sessions/*.jsonl.zstd                                          │
│   Claude Code  ~/.claude/projects/**/*.jsonl（含 subagents）                  │
│   Kimi Code  .../kimi-code/home/sessions/**/wire.jsonl                       │
└───────────────────────────────┬──────────────────────────────────────────────┘
                                │  adapters（一种会话格式一个模块）
                                ▼
                     ┌────────────────────┐   写入时带上**原始时间戳**
                     │  aml ingest / watch│───────────────────────────────┐
                     │  去重·降噪·时间回填 │                               │
                     └────────────────────┘                               ▼
                                                                 ┌──────────────────────┐
        ┌────────────────────────────────────────────────────────│  记忆服务（本机）     │
        │                                                        │  HTTP /api/* + MCP   │
        │                                                        │  SQLite + sqlite-vec │
        │                                                        │  + 本地嵌入模型       │
        │                                                        └───┬──────────┬───────┘
        │                                                            │          │
        ▼                                                            ▼          ▼
┌──────────────────┐   kind:knowledge + domain:*        ┌─────────────────┐  ┌──────────────────┐
│  aml distill     │───────────────────────────────────▶│  aml search     │  │  aml patrol      │
│  会话→跨项目知识  │                                    │  P0–P6 阶段检索  │  │  技能上游监控     │
│  （可选 LLM）     │                                    │  级联回退        │  │  干净才自动更新   │
└────────┬─────────┘                                    │  未命中必须解释   │  └────────┬─────────┘
         │                                              └─────────────────┘           │
         ▼                                                                             ▼
┌────────────────────────────────────────┐                        ┌────────────────────────────┐
│ AML_HOME/knowledge/（人能读、可搬走）   │                        │ AML_HOME/state/patrol/     │
│   沉淀/<领域>.md       索引.md          │                        │   _pending/  _backup/      │
└────────────────────────────────────────┘                        └────────────────────────────┘
```

细节与失效模式见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)（含 mermaid 版）；
检索协议（P0–P6 阶段表 + 可直接粘贴的 prompt 片段）见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)。

## 接 MCP 客户端（让别的 agent 直接用）

`aml mcp` 是一个 **stdio MCP server**（一行一个 JSON-RPC，零额外依赖），暴露六个工具：

| 工具 | 作用 |
|---|---|
| `search(query, phase, project, tag, n, allow_repeat)` | 分阶段检索；未命中会解释为什么空（冷却跳过 vs 真没有） |
| `store(content, tags, title, ktype, project)` | 写回：给了 `project` 就存项目事实，否则进沉淀层（带复核期） |
| `phase_spec(phase)` | 查某阶段的意图与预算，让 agent 自己决定给多少上下文 |
| `brief()` / `ack()` | 取/确认「这几轮新增了什么」的 ≤100 字播报 |
| `doctor()` | 体检摘要 |

```bash
# Claude Code
claude mcp add aml -- aml mcp

# 任何 MCP 客户端：command=aml, args=["mcp"]，传输 stdio
# 冒烟测试（一行一条 JSON-RPC）：
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | aml mcp
```

配合 [`docs/PROTOCOL.md`](docs/PROTOCOL.md) 里的 prompt 片段，agent 就知道**什么时候**该检索、
该要几条、查不到该怎么办。

## 设计里最重要的三个决定

**1. 时间轴要还原。** 记忆服务的写入 API 永远把 `created_at` 记成"写入时刻"，
原始时间只留在 metadata 里。不做回填，历史全挤在今天，按时间检索就是错的。
`aml sync` 每次都会把 `created_at` 回填成会话里的原始时间。

**2. 知识要分层，而且会过期。** 跨项目可复用的经验（方法论、坑、工具用法、决策理由）打
`kind:knowledge` + `reusable:true` + `domain:*`，**不打 project**；只对当前项目成立的事实打 `project:<名>`。
检索时先看沉淀层。每条知识还带 `review_after`：pitfall/tooling 180 天、pattern/checklist 365 天、
decision 730 天——技术结论会过期，`aml review` 到点提醒你复核。

**3. 检索必须能解释自己为什么空。** 只走"语义 + 硬阈值"最常见的故障是 **false empty**：
库里明明有，泛词查询却返回 0 条，调用方就误判成"没有历史约定"。所以：
级联回退（`0.80 → 0.72 → 0.65 → FTS5 关键词 → LIKE`）+ 未命中时打印分数分布 + 给出换招建议。

## 技能治理：`aml patrol`

装了很多 skill（尤其从 GitHub 抄来的）迟早遇到三件事：不知道哪些过时了、
不知道自己改过哪些、更新时怕把改动冲掉。

**三条安全闸门**（模块存在的理由）：

| 情况 | 处理 |
|---|---|
| 元数据有 `local_patch`（明确标注"我改过"） | **永不覆盖**，上游新版只暂存到 `state/patrol/_pending/` |
| `local_diff: true`，或本地内容指纹 ≠ 记录（本地被动过） | **不覆盖**，只暂存 + 播报提醒 |
| 本地干净 | 备份到 `state/patrol/_backup/` → 覆盖 → 更新元数据 |

**上游探测按代价分三层**（实战：GitHub 时通时断，API 匿名限流 60 次/小时）：

1. `git ls-remote`（~2 秒/仓库，**不吃限流**）——需要 `-c http.sslBackend=openssl`，
   否则受限环境会报 `schannel: AcquireCredentialsHandle failed`（不是网络问题，是拿不到系统凭证）
2. GitHub API `/commits/{branch}`（git 失败时兜底）
3. 内容指纹（`--deep`）：下 codeload tarball 比 `upstream_hash`；默认不开，因为有的仓库有几 MB

**结尾播报**（后台跑的东西，必须让用户在对话里看见）：

```bash
aml patrol notify --brief      # 有变化才输出：📌 技能自动更新 2 个（tdd、grilling）
aml patrol notify --ack --all  # 确认已经念给用户了，才标记（否则通知会被静默吞掉）
```

想让 agent 每次自动播报，把这条写进你的 agent 规则文件（如 `AGENTS.md`）：
「每次回答结尾执行 `aml patrol notify --brief`，有输出就贴最后一行，贴完 `--ack`」。

**定时跑**：

```bash
# Linux/macOS（crontab -e）
30 9 * * *  aml patrol run >> ~/.cache/aml-patrol.log 2>&1

# Windows（计划任务，隐藏窗口）
schtasks /Create /TN "aml-patrol" /SC DAILY /ST 09:30 ^
  /TR "cmd /c aml patrol run >> %LOCALAPPDATA%\aml-patrol.log 2>&1"
```

## 与同类项目的关系

记忆层这块已经是红海，所以先把话说清楚——**`aml` 不是另一个记忆 SDK**：

| 同类 | 它们强在 | `aml` 的差异 |
|---|---|---|
| [mem0](https://github.com/mem0ai/mem0) / [Letta](https://github.com/letta-ai/letta) | 通用记忆 SDK、agent 运行时，社区大 | `aml` 不自己实现向量库/agent 运行时，只做 **coding agent 历史的采集、蒸馏与阶段检索** |
| [Zep / Graphiti](https://github.com/getzep/graphiti) | 时序知识图谱、事实有效期 | `aml` 的时间轴是**会话原始时间**（用于回溯历史），不是事实有效期建模 |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) 等 markdown 方案 | 以 markdown 为唯一真相 | `aml` 两副面孔：向量库（快）+ markdown 镜像（可读、可搬走） |
| [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) | 后端服务本身 | `aml` **用它当后端**，补的是它没有的：多 agent 采集、蒸馏分层、阶段检索、技能治理 |

真正的差异点只有三条，也是这个仓库存在的理由：

1. **跨 agent 采集 + 原始时间戳回填**（同时覆盖 DSH / Claude Code / Kimi 三种会话格式，含 subagent）
2. **归档 → 蒸馏成跨项目可复用知识**（有分层标签、复核期、人能读的镜像）
3. **P0–P6 阶段检索协议**：把"检索"从"任务开头查一次"变成"生命周期每个节点各查各的，且各有预算"

**我们不做什么**：不做 agent 运行时、不做托管服务、不做事实有效期图谱、不做 UI。

## 信任模型

库里放着五种**信任等级完全不同**的东西：会话原文、项目事实、跨项目知识、**程序性内容**（技能正文）、
技能本身。混在一起检索是要出事的 —— 一条知识错了顶多给错信息，一条**指令**错了会直接改变 agent 的行为。

所以：跨项目知识进沉淀层最优先；程序性内容打 `kind:procedure` 并**默认不参与检索**
（只该被显式加载）；技能靠 `patrol` 管版本与覆盖保护。细节与"谁能写哪一层"见
[`docs/TRUST-MODEL.md`](docs/TRUST-MODEL.md)。

## 隐私与安全

- **采集、嵌入、检索全在本机**；只有"蒸馏"这一步会调你指定的 LLM API（可不配 = 不蒸馏）。
- 本仓库**不含任何用户内容**：状态、日志、知识库正文、会话原文都在 `.gitignore` 之外的数据目录里。
- CI 里跑一个内容泄漏扫描（`tools/scrub_check.py`）：绝对路径、凭据、邮箱、手机号、自定义屏蔽词。
  本地也能用：`python tools/scrub_check.py --staged`（只看将要提交的内容）。

## 目录

```
src/aml/
├─ config.py        配置解析（AML_HOME / config.yaml / 环境变量 / 命令行，四级覆盖）
├─ http.py          记忆服务客户端（重试、超时）
├─ text.py          截断 / 降噪判定 / 项目名归一 / 时间戳归一
├─ adapters/        agent 会话格式适配器：dsh / claude_code / kimi（新增 agent 只加一个模块）
├─ ingest.py        会话 → 记忆层（去重、降噪、时间戳回填）
├─ watch.py         常驻监听：归档触发 / 文件静默触发
├─ kb.py            知识库文档入库 + 索引重建
├─ retrieval.py     分阶段检索（级联回退 + 诊断 + 预算）
├─ distill.py       会话 → 跨项目知识（LLM 提炼 + 可读 markdown 落盘）
├─ maintenance.py   备份 / 恢复 / 导出 / 复核 / 降噪
├─ doctor.py        体检
├─ patrol/          技能治理：github 探测 / skills 指纹与镜像 / update 安全闸门
│                   / packages 包版本监控 / notify 播报队列
├─ patrol_cli.py    `aml patrol` 子命令
└─ cli.py           命令行入口
```

## 开发

```bash
pip install -e ".[dev]"
pytest -q                       # 测试（fixture 全部是合成数据，不含真实会话）
ruff check src tests tools      # lint
python tools/scrub_check.py     # 提交前查内容泄漏（CI 也会跑）
```

**提交统一走守门器**（多个 agent 会话共用同一个工作目录时的安全阀，详见 [`AGENTS.md`](AGENTS.md)）：

```bash
python tools/repo_guard.py --status                                   # 先看状态
printf 'fix: ...\n' > msg.txt
python tools/repo_guard.py --message-file msg.txt --paths src/aml/x.py tests/test_x.py
```

它加锁串行化（`.git/repo_guard.lock`）、`git fetch` 对表（落后/分叉就拒绝）、
**拒绝 `git add -A`**（必须显式列路径，且暂存区要先为空）、跑测试与 lint 与泄漏扫描，最后提交推送。
不是自己改的文件：写交接单 `python tools/repo_guard.py --handoff "改了什么" --paths <文件>`，把提交留给主控。

**改代码前请再跑一遍 3.9**（最低支持版本）。CI 的 3.9 矩阵抓到过 `Path.write_text(newline=)`
（3.10+ 才有）这类问题——本地只有 3.12 时永远看不见：

```bash
uv venv --python 3.9 .venv39 && uv pip install --python .venv39/bin/python -e ".[dev]" zstandard
.venv39/bin/python -m pytest -q          # Windows 把 bin/ 换成 Scripts/
```

跨平台行为（路径分隔符、时钟粒度、换行符）也请在 CI 矩阵上确认，别只在本地一个系统上过。

想新增一个 agent 适配器：在 `src/aml/adapters/` 加一个模块，实现 `discover()`（找出会话文件）
与 `turns()`（产出 `Turn`），然后在 `adapters/__init__.py` 注册一行，并补一个用合成数据的测试。

## 许可证

MIT，见 [`LICENSE`](LICENSE)。
