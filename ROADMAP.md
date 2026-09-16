# ROADMAP

从一台**真实在用的机器**上抽取这套系统的过程，分三步。`[x]` = 已完成并在本机验证过。

## Step 1 拆仓去内容

- [x] 新仓库骨架：`src/aml/`、`tests/`、`tools/`、`.github/workflows/`、`pyproject.toml`、`LICENSE`(MIT)、`.gitignore`
- [x] **配置层**：`AML_HOME` + `config.yaml` + 环境变量 + 命令行，四级覆盖；程序里不再出现绝对路径
- [x] **adapter 化采集**：`dsh`（zstd JSONL）/ `claude_code`（含 subagent 标记）/ `kimi`（过滤内部标题生成）
- [x] 无关项目代码**不进仓**：客户线索库、飞书同步、抓取脚本等留在原处，本仓库只有记忆层机制
- [x] 从实战长出来的规则进代码：噪声过滤（界面回显/纯确认语）、正文截断上限、原始时间戳

## Step 2 可安装可验证

- [x] `aml` CLI：`init` / `doctor` / `sync` / `watch` / `distill` / `search` / `ingest-kb` / `index`
      / `backup` / `restore` / `export` / `review` / `denoise`
- [x] **体检** `aml doctor`：目录、服务、向量覆盖、FTS、**索引新鲜度（比对索引自报数 vs 库内数）**、
      各 adapter 能否看到会话、泛词检索自测 —— 每项都带"怎么修"
- [x] **合并检索栈**：`kb_lookup` 的阶段预算 + `recall_plus` 的级联回退合成 `retrieval.py`
      （0.80→0.72→0.65→FTS5→LIKE，未命中必须解释为什么空）
- [x] **`aml backup` / `aml restore`**：在线备份 + 恢复前预检（integrity_check + 条数）
      + 恢复前自动另存现有库 —— **补上了原系统"只有备份没有恢复"的缺口**
      （顺带修掉一个真 bug：同一秒内连续备份会互相覆盖，现在文件名带微秒）
- [x] `aml export`：导出成人可读 markdown（按领域分组）/ 原始 JSON
- [x] `aml watch`：常驻监听（DSH 归档即时入库 / 会话静默 ≥stable 秒增量入库），
      状态存 `state/watch_state.json`；蒸不蒸按 agent 分开（kimi 只入库、子代理不蒸、DSH 要求 ≥3 轮）
- [x] `aml distill`：会话 → 跨项目知识（`kind:knowledge`+`domain:*`、复核期、互斥锁、
      增量重蒸门槛、逐会话落盘、输入截断防"吃光输出预算返回空"）
- [x] `aml review` / `aml denoise`（软删除可回滚）/ `aml ingest-kb`（`--since` 增量）
- [x] pytest **35 个测试**（合成 fixture，含备份↔恢复往返、坏备份拒收、队列增量重蒸、监听冷却判定）
- [x] `ruff` 全绿；GitHub Actions：3 系统 × 2 Python 版本跑 lint+test，外加**内容泄漏扫描**独立 job
- [x] `tools/scrub_check.py`：绝对路径 / 凭据 / 邮箱 / 手机号 / 自定义屏蔽词；默认只扫"会被提交的文件"
- [x] 本机对着线上数据验证：`doctor` 13 项 0 失败、检索命中跨项目沉淀、`sync --dry-run` 采到 4627 条、
      `backup` 真实备份 68.3 MB / 9720 条并 integrity ok、`export` 导出 9720 条（沉淀 482 / 146 领域）、
      `watch --seed --dry-run` 看到 223 个会话文件与 49 个已归档会话

### 还没搬过来的（下一步）

- [x] **`patrol` 技能治理**：`aml patrol sync / adopt / check / update / accept / packages / notify / run`
      —— 镜像入库 + 重建清单、上游 commit 探测（git ls-remote → API → 内容指纹三层）、
      **三条安全闸门**（本地补丁与本地改动永不覆盖，只暂存）、包版本监控（只监控不升级）、
      ≤100 字结尾播报队列。本机实测：`patrol check` 认出 28 个受跟踪技能全部最新；
      `patrol sync` 重建清单（46 个技能 / 跟踪 28 / 本地补丁 1），与旧系统数字一致
- [ ] MCP server：把 `search` / `distill` 暴露给任何 MCP 客户端
- [ ] `embedding backfill`：补齐缺失向量的记录（缺失 = 永远搜不到）
- [ ] `dedup`：近义知识合并（带回滚）—— 现在只有 `denoise`（噪声），没有去重
- [ ] 可选适配器：Codex CLI / Copilot Chat / Cursor（本机实测这三家当前没有可用会话数据）
- [ ] `watch` 的守护自愈（原系统用 VBS + `watch-forever.ps1` 保持常驻；跨平台方案待定）
- [ ] 技能清单的差异视图（`patrol diff`：把"上游改了什么"直接摆出来，而不只是暂存）

## Step 3 差异化（发布后要打的牌）

- [ ] **跨 agent 时间轴**：写成"如何新增一个 adapter"的文档 —— 这是最容易吸引贡献者的接口
- [ ] **蒸馏 → 分层知识**：把"会话变可复用经验"做成 MCP 工具，而不只是本地 CLI
- [ ] **P0–P6 阶段检索协议**：写成 SPEC + 可直接粘贴的 prompt 片段（方法论 + 实现双份）
- [ ] **技能治理**独立成小项目（受众明确：装了很多 skill 的多 agent 用户）
- [ ] 基准对比：在这套数据上量一下"有记忆 vs 没记忆"的任务成功率/返工次数

## 发布前 checklist

- [ ] `pyproject.toml` 里 `OWNER` 占位符换成真实 GitHub 用户名
- [ ] 决定仓库名（当前 `agent-memory-layer`）与许可（当前 MIT；若在意专利授权可换 Apache-2.0）
- [ ] git 身份：本仓库当前是占位 `your-name <you@example.com>`，改成你自己的
- [ ] 跑一遍 `python tools/scrub_check.py --all` 确认连被忽略的文件里也没有内容
- [ ] README 补架构图与一段 30 秒演示（`aml doctor` → `aml search`）
- [ ] 想清楚怎么描述与上游 `mcp-memory-service` 的关系（我们是它的"采集+蒸馏+阶段检索"上层）
