# ROADMAP

从一台**真实在用的机器**上抽取这套系统的过程。`[x]` = 已完成并在本机验证过。
> 维护约定：**这一页必须与 README 的能力描述同步**。曾经的教训是 README 已经把 `patrol`/`mcp`
> 当现有能力介绍，而这里还把它们留在"未完成"区 —— 读者无法判断哪个是权威状态。
> 改完功能就顺手改这里（`tools/repo_guard.py` 提交前会跑测试，但不检查文档一致性，
> 所以这条靠自觉 + review）。

## Step 1 拆仓去内容 ✅

- [x] 新仓库骨架：`src/aml/`、`tests/`、`tools/`、`.github/workflows/`、`pyproject.toml`、`LICENSE`(MIT)、`.gitignore`
- [x] **配置层**：`AML_HOME` + `config.yaml` + 环境变量 + 命令行，四级覆盖；程序里不再出现绝对路径
- [x] **adapter 化采集**：`dsh`（zstd JSONL）/ `claude_code`（含 subagent 标记）/ `kimi`（过滤内部标题生成）
- [x] 无关项目代码**不进仓**：客户线索库、飞书同步、抓取脚本等留在原处，本仓库只有记忆层机制
- [x] 从实战长出来的规则进代码：噪声过滤（界面回显/纯确认语）、正文截断上限、原始时间戳

## Step 2 可安装可验证 ✅

- [x] `aml` CLI：`init` / `doctor` / `sync` / `watch` / `distill` / `search` / `ingest-kb` / `index`
      / `backup` / `restore` / `export` / `review` / `denoise` / `dedup` / `backfill-embeddings` / `mcp`
- [x] **体检** `aml doctor`：目录、服务、向量覆盖、FTS、**索引新鲜度**、各 adapter 能否看到会话、
      泛词检索自测 —— 每项都带"怎么修"
- [x] **合并检索栈**：阶段预算 + 级联回退（0.80→0.72→0.65→FTS5→LIKE）+ **未命中必须解释**
      （并且区分"冷却跳过"与"真的没有"）
- [x] **`backup` / `restore`**：在线备份 + 恢复前预检（integrity_check + 条数）+ 恢复前另存现有库
- [x] `watch`（归档即时入库 / 会话静默增量入库）、`distill`（会话→跨项目知识）、`export`、
      `review`、`denoise`（软删除）
- [x] **`patrol` 技能治理**：`sync / adopt / check / update / accept / packages / notify / run`
      —— 镜像入库 + 重建清单、上游 commit 探测（`git ls-remote` → API → 内容指纹三层）、
      **三条安全闸门**（本地补丁与本地改动永不覆盖，只暂存）、包版本监控、≤100 字结尾播报
- [x] **MCP server**（`aml mcp`，stdio JSON-RPC，零额外依赖）：`search / store / phase_spec /
      brief / ack / doctor` 六个工具；`store` 强制分层（`project` → 项目事实，否则进沉淀层带复核期）
- [x] **`dedup`**：二元组 Jaccard 聚簇 + 领域约束 + LLM 合并 + **整簇快照可回滚**（默认只预览）
- [x] **`backfill-embeddings`**：补齐缺向量的记录（必须带 `conversation_id`，否则被语义去重拒收）
      + `--prune-orphans`
- [x] **`repo_guard`**：多会话共享工作区时的提交守门器（加锁串行化、拒绝 `-A`、对表远端、跑守门）
- [x] pytest **85+ 个测试**（合成 fixture；含备份↔恢复往返、坏备份拒收、队列重蒸、
      跨平台路径/时钟粒度回归、守门器拒绝越权暂存）
- [x] `ruff` 全绿；GitHub Actions：3 系统 × 2 Python 版本（**含 3.9**）+ 内容泄漏扫描 + 3.9 编译兜底；
      **CI 绿**（badge 实测 passing）
- [x] 本机对着线上数据（只读）验证：`doctor` 0 失败、`search` 命中跨项目沉淀、
      `backup` 68.3 MB / 9720 条 integrity ok、`export` 9720 条、`watch --seed` 223 个会话文件、
      `dedup` 预览 454 条知识 → 8 个重复簇、MCP stdio 冒烟 6 个工具

## Step 3 下一步：信任与治理（当前主线）

> 这一节来自一次外部 review（2026-09-17）。我按"是否真的成立"筛过一遍：
> 成立且优先的排在前面，标注了哪些是**现在就有的实现**、哪些是**缺口**。

### 3.1 信任模型（最先做，其它都依赖它）

- [x] `docs/TRUST-MODEL.md`：把 `observation / memory / knowledge / procedure / skill / policy`
      六种东西分清，给出信任阶梯与**谁能写哪一层**的权限通道
- [x] **技能内容不得默默进入通用检索**：`kb.ingest_docs` 对 `ingest.procedure_dirs`（默认 `技能原始`）
      里的文档打 `kind:procedure` + `authority:procedure`；`Retriever.search` 默认过滤掉它们，
      `--include-procedure` / MCP `include_procedure: true` 才返回；未命中时明确说明"有 N 条程序性内容被跳过"
- [x] **存量迁移**：`aml migrate procedure`（默认只预览）。要点：服务的 content_hash **把标签算进去了**，
      所以迁移 = **重存 + 删旧**；重存必须带 `conversation_id`（否则被判重复而拒收）；
      先写整批快照，可 `aml migrate rollback --file <快照>`。本机规模：2804 块技能内容。
      另外**检索层同时认 `kb:<程序性目录>`**，所以即使一块都没迁移，技能正文也不会被自动召回——
      迁移的意义是让数据本身带上标签（给别的消费者/导出看），不是安全兜底的唯一依赖
- [ ] **authority 维度**：检索排序目前只有 `语义分 × 阶段预算 × 层级`。
      加入 `authority`（谁写的：人审 / 蒸馏 / 会话原文 / 技能）与 `freshness`，并在输出前缀里显示
- [ ] **写权限分层**：MCP `store` 目前 agent 可自由写 `kind:knowledge`。
      应该：会话事实可自由写 → 知识候选需复核标记 → 技能/策略候选必须人工批准
- [ ] **provenance 成为一等字段**：已记录 `src_session`/`evidence`；还缺 `derived_by`
      （模型 + prompt 版本）与 `source_turns`，排查"这条到底谁说的"时需要

### 3.2 记忆质量闭环（现在只有 review_after，没有反馈）

- [x] **outcome 反馈（核心）**：`aml feedback --hash H --outcome worked|failed|used`（MCP 也有 `feedback` 工具），
      字段 `usage_count / success_count / failure_count / last_used_at / last_failed_at`；
      可靠度用拉普拉斯平滑（1 次成功 ≠ 满分），
      **只在同档位内重排**（分数门槛不动，新记忆不因"没用过"被挤出，失败记忆不消失只降权）；
      `review --verify <hash>` 写 `last_verified_at` 并顺延复核期
- [ ] **反馈的自动采集**：现在要 agent 显式打点。下一步：从任务结果里推断（P6 写回时顺带更新），
      或让 `aml watch` 在会话结束时按"是否复用了某条记忆"自动打 `used`
- [ ] **否定记忆与冲突关系**：`supersedes / contradicts / deprecated_by`。
      现状只有"以时间较新为准"的合并口径，缺显式建模（否则"用 Redis"和"别用 Redis"会同时命中）
- [ ] **统一 ChangeSet 回滚**：skill 更新、镜像、索引、通知队列目前各自回滚，容易只回滚一半

### 3.3 技能治理 → 完整生命周期（现在只是"更新治理"）

- [ ] **状态机**：`discovered → tracked → candidate → scanned → approved → active →
      deprecated → disabled → retired`（现在只有 tracked / clean / modified / staged）
- [ ] **能力声明 + 风险扫描**：`SKILL.md` 旁边加 `skill.yaml`（capabilities: shell/network/
      filesystem/secrets + side_effects + scope + requires_approval），更新时做**能力差异**而不是只看 commit
- [x] **`patrol diff`**：`accept` 之前看清"上游改了什么" —— 文件级增删改 + 正文统一 diff +
      **能力信号差异**（网络请求 / shell 调用 / 读密钥 / 写删文件 / git 写操作 / 装依赖 / 浏览器
      自动化；裸链接单列"留意"）。默认**不联网**（审已暂存的 `_pending/`），`--fetch` 才去上游取快照；
      `--fail-on-risk` 在有新增高风险信号时退出码 2（可做门禁）
      —— 实测教训：第一版把 Markdown 引用块 `>` 当成写文件、行内反引号当成 shell，
      这种"狼来了"等于没有信号，已收紧成**只认可执行的调用形态**（并加了回归测试）
- [ ] **来源可信度**：记录 repo/ref/commit/sha256/publisher，支持"只信白名单来源"

### 3.4 工程与可复现

- [ ] **后端契约固定**：`mcp-memory-service` 的 API/schema/嵌入模型版本写进 doctor 与文档
      （现在只有 `db_path` 与 API 地址，后端悄悄变会导致检索语义漂移而 AML 看不出来）
- [ ] **蒸馏前脱敏**：`dirty→LLM` 目前直发会话原文；`scrub_check` 只管仓库内容泄漏，
      不是"发送前脱敏层"。需要敏感信息（key/token/客户名/内网地址）在调用前打码
- [ ] **watch 的会话边界**：DSH 有归档事件（强信号），其他 agent 只有"静默 ≥120s"启发式；
      长思考（>120s）会被误判成会话结束 → 需要结合 agent 生命周期事件
- [ ] **多会话并发不变量**：git 已有 `repo_guard`；memory/distill/patrol/backup 的
      ownership / 幂等 / 事务边界还没定义
- [ ] 可选适配器：Codex CLI / Copilot Chat / Cursor（本机实测这三家当前没有可用会话数据）
- [ ] `watch` 的守护自愈（原系统用 VBS + `watch-forever.ps1`；跨平台方案待定）

## Step 4 传播与验证（发布后）

- [ ] **基准对比**：`memory ON/OFF` + `skill governance ON/OFF`，量任务成功率、返工次数、
      到解时间、token 消耗、错误记忆率、技能回退率。**没有这个，README 的定位就只是主张**
- [ ] **一条命令接入**：`pipx install` + `aml init` 后自动发现各 agent、首次 sync、首次 recall
      （目标是 10 分钟内完成第一次成功召回）
- [ ] `aml memories` / `aml why <hash>`：让人能看见"库里有什么、为什么召回它"
- [ ] **定位收敛**：一句话从"另一个 agent memory"改成
      **"coding agent 的记忆 + 技能治理"**（README 首屏按这个重写）
- [ ] 技能治理独立成小项目（`skill-patrol`：受众明确，撞车概率低）
- [ ] 跨 agent 时间轴的"如何新增 adapter"文档（最容易吸引贡献者的接口）

## 发布前 checklist

- [x] `pyproject.toml` 占位符换成真实 GitHub 用户名（kh859278）；仓库名与许可（MIT）已定
- [x] git 身份：提交作者改为 `jk <293333882+kh859278@users.noreply.github.com>`
- [x] 已推送到 GitHub（SSH），CI 绿
- [x] README：徽章 + 真实输出 + 同类对照 + 诚实安装说明（PyPI 未发布）+ 架构图 + 开发入口
- [x] 写清与上游 `mcp-memory-service` 的关系
- [ ] 跑一遍 `python tools/scrub_check.py --all` 确认连被忽略的文件里也没有内容
- [ ] 仓库 description 与 topics（`ai-agents` `memory` `mcp` `claude-code` `knowledge-base`）；
      **建议加 `skill-governance`**（这是差异化所在）
- [ ] 发布到 PyPI（`pipx install agent-memory-layer` 是 README 里承诺的下一步）
