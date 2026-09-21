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
- [x] pytest **150+ 个测试**（合成 fixture；含备份↔恢复往返、坏备份拒收、队列重蒸、
      跨平台路径/时钟粒度回归、守门器拒绝越权暂存、任务级基准的判分口径与状态机迁移）
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
- [x] **反馈的自动采集**：两条路都通了。
      ① **任务级基准**（`aml bench --task-level --feedback`）按证据回写：只有"这条记忆自己的
      正文里出现了被用上的做法"才记 `worked`，出现任务声明的过期做法记 `failed`，其余只记 `used`
      —— 不搞连坐（任务失败不会把所有注入的记忆一笔勾成 failed）
      ② **召回账本**（`state/recall-log.jsonl`，append-only、只记 hash/字数/阶段/查询、
      **不记正文**、写失败不影响检索）+ `aml feedback --last 1 --outcome worked`：
      免去"让 agent 自己抄 hash"这一步（它手上只有渲染后的文本，逼它抄就是逼它编）。
      `--list` 看最近几次注入了什么，`--dry-run` 先预演。
      仍缺的一步：**从任务结果自动判定成败**（现在成败信号来自基准的验收命令或人来判断）
- [ ] **否定记忆与冲突关系**：`supersedes / contradicts / deprecated_by`。
      现状只有"以时间较新为准"的合并口径，缺显式建模（否则"用 Redis"和"别用 Redis"会同时命中）
- [ ] **统一 ChangeSet 回滚**：skill 更新、镜像、索引、通知队列目前各自回滚，容易只回滚一半

### 3.3 技能治理 → 完整生命周期（现在只是"更新治理"）

- [x] **状态机**：`discovered → tracked → candidate → scanned → approved → active →
      deprecated → disabled → retired`（`state/patrol/lifecycle.json`，每次变更留 history：
      谁/何时/为什么/是否越级）。口径：**只有 `approved` / `active` 允许被自动更新**，
      其余状态一律只暂存待批；`deprecated`/`disabled`/`retired` 不更新也不加载。
      老技能不会被"升级当天全拦一轮"——没登记过的会按本地/上游状态**就地推断登记**
      （受跟踪 + 本地干净 = `active`，有本地改动 = `candidate`，有待批上游版 = `scanned`）。
      CLI：`aml patrol lifecycle [name] [--set STATE --why …]`
- [x] **能力声明 + 风险扫描**：`SKILL.md` 旁边加 `skill.yaml`
      （`capabilities: [shell|network|secrets|filesystem-write|git-write|install|browser|url]`
      ＋ `scope` / `requires_approval` / `authority`），与 `patrol diff` 的实测信号直接对比。
      **闸门第 4 条**（加在原有三条安全闸门之后）：本地干净也不一定自动覆盖 ——
      生命周期状态不在 `approved/active`、声明了 `requires_approval`、
      或**上游新版新增了未声明的高风险能力** → 只暂存（动作记为 `gated`，播报里也会说）。
      人采纳（`aml patrol accept`）即把生命周期推到 `approved` —— 那道闸门的出路就是人。
      CLI：`aml patrol capabilities [name] [--json]`（看声明 vs 实测，没声明的风险单列）
- [x] **批准要带证据 + 证据分级**（2026-09-21，外部评审的第二轮）：
      ① **证据分级**：`declared`（skill.yaml 声明）> `instructional`（文本在要求执行：
      "运行/执行/run the following"，或紧跟在祈使句后的代码块）> `example`（明确标了
      "例如/示例/Example" 的代码块）> `mention`（只是提到）。**闸门只认前两级** ——
      示例里的 `curl`/`rm -rf` 不再触发拦截（第一版把 Markdown 引用块 `>` 当写文件、
      行内反引号当 shell，那种"狼来了"的信号等于没有信号）
      ② **批准绑定内容 hash**：`record_approval` 写 `baseline_hash`；本地内容变过 → 批准作废，
      重新过目（`gate` 会拦）
      ③ **能力历史取并集**：敏感能力"删掉又回来"也要重新批准，不能靠"本次 diff 里没有新增"蒙过去
      ④ **审的与采纳的必须是同一版**：`patrol diff` 记下暂存内容指纹（`state/patrol/reviewed.json`），
      `accept` 核对；对不上就跳过（否则会出现"审的是 A、批准的是 B"）
      ⑤ **accept 摆证据**：打印能力差异与等级变化，有新增未声明高危能力时必须 `--yes`；
      生命周期状态只提示不拦（`accept` 本身就是人的批准动作）
      ⑥ 已知边界（诚实写下来）：我们**不控制技能的执行层** —— 技能是给 agent 看的文本，
      真正跑 shell/git 的是宿主 agent 的权限系统。所以这套闸门保证的是
      "**未经重新批准的内容变化不能自动进入受治理状态**"，不是"技能执行时不会做危险操作"
- [x] **`patrol diff`**：`accept` 之前看清"上游改了什么" —— 文件级增删改 + 正文统一 diff +
      **能力信号差异**（网络请求 / shell 调用 / 读密钥 / 写删文件 / git 写操作 / 装依赖 / 浏览器
      自动化；裸链接单列"留意"）。默认**不联网**（审已暂存的 `_pending/`），`--fetch` 才去上游取快照；
      `--fail-on-risk` 在有新增高风险信号时退出码 2（可做门禁）
      —— 实测教训：第一版把 Markdown 引用块 `>` 当成写文件、行内反引号当成 shell，
      这种"狼来了"等于没有信号，已收紧成**只认可执行的调用形态**（并加了回归测试）
- [ ] **来源可信度**：记录 repo/ref/commit/sha256/publisher，支持"只信白名单来源"

### 3.4 工程与可复现

- [x] **作用域（scope）**：`patrol.scopes` 里一个作用域可以是 `global` 或 `project:<名>`，
      每个作用域自由布置多个技能目录（项目里写相对项目根的路径）。每个目录一个 `sync_kb`：
      决定"正文要不要也进知识库"（项目里的技能默认**不进**，只在本地）。
      老配置零改动：没写 `scopes` 就按 `skill_roots` 派生一个 global 作用域。
      CLI：`aml patrol scopes`
- [x] **来源模型 + 仓库布局识别**：`state/patrol/sources.json`（repo/scope/layout/subdir/
      branch/enabled/priority）；布局按 `root`（仓库根就是一个技能）/ `standard`（`skills/<名>`）/
      `template`/`flat`/`nested`（`plugins/x/skills/<名>`）识别。
      `adopt` 不再用"目录名命中 ≥3 个就认仓库"的猜法，改成**归一化精确同名**匹配
      （`Skill-Name` == `skill_name`）——猜错的代价是拿别人的内容覆盖你的文件。
      CLI：`aml patrol sources list|add|remove|enable|disable|detect --fetch`
- [x] **安装 / 卸载**（对标 SkillTruck / skill-manager 的核心能力）：
      `aml patrol install <技能...> [--scope|--project] [--source] [--dry-run] [--force]`
      / `aml patrol uninstall`。三段式：`plan_install` 只读 → `apply_plan` 才动盘
      （所以 `--dry-run` 是真的什么都不改）。约定沿用现有安全闸门：**覆盖/卸载前一定先备份**；
      目标已有同名技能且**本地改动过** → 默认拦下（`--force` 才覆盖）。
      一个目标失败不影响其它目标（逐个 try）
- [x] **主机白名单显式化**：`patrol.allowed_hosts`（默认 github/codeload/api/raw 四个主机）。
      以前"只信 github.com"是隐含在拼 URL 的代码里，现在**可配置、可审计**，
      非白名单直接拒绝并说清怎么加；仓库标识必须是 `owner/repo`（不接受 URL）
- [x] **仓库快照缓存**：tarball 落在 `state/patrol/_cache/`，TTL（默认 900s）内不重复下载，
      `--force-refresh` 跳过；解包永远到新的临时目录（调用方 `rmtree` 不会误删缓存）。
      定时任务从"每轮每仓库都下几 MB"变成"TTL 内零下载"
- [x] **锁文件与状态总览**：`aml patrol lock [--write]` 导出 `skills-lock.json`
      （`lockfileVersion`/生成器/作用域/每个技能的 repo·subdir·commit·content_hash·
      upstream_hash·local_diff·targets·是否进知识库）—— 对标 `.skill-lock.json` 与
      biw 的 `skills-lock.json`（后者被扫描器当作"这是个技能项目"的标记）。
      `aml patrol status` 给人类视图：已纳管/本地改动/待批/未纳管一眼看清。
      锁文件里**只有元数据与哈希，没有技能正文**（有测试钉着这条）
- [x] **布局识别补一档 `categorized`**：真实仓库 `mattpocock/skills` 用的是
      `skills/<类别>/<名>`（如 `skills/engineering/tdd`），原先被笼统报成 `nested`，
      看报告的人分不清"组织方式"还是"藏在角落"（这是对真机跑 `patrol status` 时发现的）
- [ ] **后端契约固定**：`mcp-memory-service` 的 API/schema/嵌入模型版本写进 doctor 与文档
      （现在只有 `db_path` 与 API 地址，后端悄悄变会导致检索语义漂移而 AML 看不出来）
- [ ] **蒸馏前脱敏**：`dirty→LLM` 目前直发会话原文；`scrub_check` 只管仓库内容泄漏，
      不是"发送前脱敏层"。需要敏感信息（key/token/客户名/内网地址）在调用前打码
- [ ] **watch 的会话边界**：DSH 有归档事件（强信号），其他 agent 只有"静默 ≥120s"启发式；
      长思考（>120s）会被误判成会话结束 → 需要结合 agent 生命周期事件
- [x] **不可重建运行数据的防护**：`state/` 被 gitignore、没有 git 历史 —— 2026-09-18 19:40
      整棵 `state/` 被删掉重建（目录 CreationTime 为证），9/17 的任务级基准报告、召回账本、
      基准任务表全丢，**三天后才发现**（当时 `doctor` 完全不知道）。现在：
      ① `state/README.md` 写清哪些不可重建（`aml state readme`，巡检会重建）；
      ② 巡检每天 `aml state snapshot` 到 `backups/state/<时间戳>/`（保留最近 10 份）；
      ③ `aml doctor` 盯「以前快照里有、现在没了」= 确切的"被删了"信号，而不是"新装机器上没有"；
      ④ 新的不可重建数据要加进 `src/aml/state_guard.py` 的 `DURABLE`
- [ ] **多会话并发不变量**：git 已有 `repo_guard`；memory/distill/patrol/backup 的
      ownership / 幂等 / 事务边界还没定义。**已知缺口**：`repo_guard` 的锁只覆盖提交路径，
      不覆盖"改工作树"阶段（A 跑测试时 B 改了同一个文件，A 的测试结果其实测的是 B 的文件）；
      现实替代是 per-session `git worktree` + `state/` 快照（见上一条），
      因为宿主 agent 可以自由执行 shell，任何"写入前检查"都拦不住直接写盘
- [ ] 可选适配器：Codex CLI / Copilot Chat / Cursor（本机实测这三家当前没有可用会话数据）
- [ ] `watch` 的守护自愈（原系统用 VBS + `watch-forever.ps1`；跨平台方案待定）

## Step 4 传播与验证（发布后）

- [x] **基准（检索层）**：`aml bench --tasks <任务表>` —— memory ON/OFF 对照，量
      **入口命中率**与**平均注入字数**（`--min-hit-rate` 可做回归门禁）。
      本机首跑：12 个任务（查询取自已沉淀知识）**12/12 命中，平均注入 421 字**。
      ⚠️ 说清口径：这一层证明"入口覆盖得住、上下文不贵"，**不等于**任务成功率；
      首跑用的是标题当查询（偏易），真实评估应写**改写过的问法**。
      任务表放 `$AML_HOME/state/bench-tasks.jsonl`（不进仓库），模板见 `tools/bench/tasks.example.jsonl`
- [x] **基准（任务层）**：`aml bench --task-level` —— 真起 agent 跑同一批任务两遍
      （有记忆/无记忆），判成败并统计：成功率、返工（轮数+改动文件）、耗时、
      token/成本、被记忆带偏率（违禁串）、既有功能回退率；`--feedback` 顺手把结果
      按证据回写成记忆反馈；有回退/违禁则退出码非 0（可做 CI 门禁）。
      关键机制：fixture 的 `_hidden/`（验收标准判分前才拷进工作区，agent 看不到）——
      这是唯一能区分"记得"与"猜得到"的手段。任务表不进仓库，模板见
      `tools/bench/task-level.example.jsonl`，方法论与边界见 `docs/TASK-BENCH.md`。
      **首跑实测（3 任务 × 2 组，$1.79）**：OFF/ON 都 100% 成功、0 违禁、0 回归，
      成功率差值 +0%，token 差值 −116（噪声级）—— 诚实结论是：
      **在这些"能从仓库推出做法"的任务上，记忆没改变成败**；
      要证明价值必须挑"仓库里没有、prompt 里也不说"的约定型任务（下一批）
- [ ] **一条命令接入**：`pipx install` + `aml init` 后自动发现各 agent、首次 sync、首次 recall
      （目标是 10 分钟内完成第一次成功召回）
- [ ] `aml memories` / `aml why <hash>`：让人能看见"库里有什么、为什么召回它"
- [ ] **定位收敛**：一句话从"另一个 agent memory"改成
      **"coding agent 的记忆 + 技能治理"**（README 首屏按这个重写）
- [ ] 技能治理独立成小项目（已建预留仓 [`skill-patrol`](https://github.com/kh859278/skill-patrol)，
      待依赖解耦后搬迁）
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
- [ ] **发布到 PyPI（`pipx install agent-memory-layer` 是 README 里承诺的下一步）**
      ⚠️ **名字已被占用（2026-09-18 实测）**：PyPI 上的 `agent-memory-layer` 是 SAP 的包
      （"A reusable memory layer for SAP agentic workflows"，0.1.0/0.1.1，2026-04 上传）。
      所以：① 发布前必须先换名（例如 `agent-memory-layer-aml`），否则发不上去；
      ② `aml self-update` 已加**归属校验**（PyPI 那份不是我们的就拒绝，退查 git tag）；
      ③ README 里"pipx install agent-memory-layer"这句话现在是**错的**，换名后同步改
- [x] **只读本地视图（`aml patrol ui`）**：把作用域/来源/生命周期/待批/最近报告/基准汇总
      成一个单文件静态 HTML（无外链、无 JS 依赖、**不含技能正文**）。不做交互式 Web UI：
      这台机器上的入口是 09:30 的计划任务，非交互优先
- [x] **profile + 配置同步**：`aml patrol profile save/list/show/remove`（技能集 + 作用域），
      `aml patrol install --profile <名>` 按来源分组复现整套；`aml patrol config push/pull`
      支持本地路径与 git 仓库两种传输（HTTPS 受主机白名单约束，同名 profile **冲突不覆盖**）
- [x] **自查更新（`aml self-update`）**：默认只查不装（自升级不可逆，且它是计划任务在跑的东西）；
      识别 pipx/uv/pip 三种安装方式给对应命令；**归属校验**后发现 PyPI 同名包是别人的，
      于是 `auto` 会退回查我们自己仓库的 tag
