# 推广清单（不发论坛版）

前提：**不去论坛/社群发帖**。那就别指望"被看到"，改成"**被搜到**"。三条路，按 ROI 排：

```
站内可搜索性  →  包/目录索引  →  自有阵地 SEO  →  上游互链与引用  →  5 分钟转化
（不需要账号）    （需要账号）      （你自己的站）    （PR，不是发帖）   （已基本完成）
```

---

## A. 站内可搜索性（不需要账号，我已完成一半）

GitHub 的发现机制靠 **description + topics + 首屏关键词**，三处都要对齐同一句话。

**description（复制粘贴到仓库右上角 About）**

```
Local-first shared memory + skill governance for multiple coding agents
(Claude Code / Kimi Code / DeepSeek Harness). MCP-ready, no cloud.
```

**topics（12 个，按这个顺序填）**

```
agent-memory  cross-agent-memory  coding-agents  claude-code  mcp  mcp-server
skill-governance  ai-agents  knowledge-base  local-first  python  llm-tools
```

**首屏**：README 已重写为"**Multiple coding agents. One shared memory. One skill state.**" +
中英双写 + 一张五环节表 + 三个入口链接（QUICKSTART / FAQ / PROTOCOL）。
> 用户需要在网页上点两下（或给一个带 `repo` 权限的 token，我一次做完）。

## B. 包与目录索引（需要账号/凭据）

1. **PyPI**：✅ **已发布 `aml-memory 0.1.0`**（2026-09-22）
   → https://pypi.org/project/aml-memory/ 。安装即 `pipx install aml-memory`。
   PyPI 页面本身会被搜索引擎与 LLM 抓取；**没有文章时，这里是唯一被动的曝光位**。
   首次上传的实现细节（token 长度校验、403 的两种成因）记在 [`RELEASING.md`](../RELEASING.md)。
2. **MCP 目录/registry**（这是"提交目录"，不是发帖）：官方 MCP servers 列表、
   mcp.so、Smithery、Glama、PulseMCP 之类。需要准备的材料都差不多：
   一句话定位 / 仓库地址 / 安装命令 / `aml mcp` 的启动方式 / 传输方式（stdio）/ 权限说明。
   这些材料我可以在你给了账号后逐条填。

## C. 自有阵地 SEO（内容我来写，发布你来）

写"问题型"文章而不是"项目宣传"——别人搜的是问题，不是你的项目名。

1. **多个 coding agent 为什么记不住彼此学到的东西** — 论点：每个 agent 的历史锁在自己目录里；
   给出采集/回填原始时间戳的做法与坑。
2. **把 9772 条记忆接在 coding agent 后面：哪些真的被用上了** — 给出真实数字：
   检索层 P@3、注入量 P50/P95、任务级基准 **成功率差值 +0%（$1.79）**、
   以及"为什么 +0% 也要写出来"。**这篇的可信度来自不利数字。**
3. **三个 agent 共用一个 git 工作区，会坏在哪** — 讲 `repo_guard`、锁只覆盖提交不覆盖改工作树、
   以及"`state/` 被当成缓存删过一次"的真实事故。

每篇的英文版放站点上（LLM 语料以英文为主），中文版放你自己的渠道。
**只发你自己的阵地，不需要进任何社区。**

## D. 上游互链与引用（PR 不是发帖）

- `mcp-memory-service`：给它提一个 PR/issue，把本项目加进"谁在用"（我们依赖它的 HTTP+MCP 后端）
- 与本机另一个仓库 [`skill-patrol`](https://github.com/kh859278/skill-patrol) 互链（已做）
- `CITATION.cff`（已加）——让"怎么引用"有标准答案
- README 顶部那句定位就是给 LLM 抓取的**可引用定义**，别再改成长句

## E. 5 分钟转化（已完成）

- [`docs/QUICKSTART.md`](QUICKSTART.md)：装 → init → 起后端 → doctor → 首次召回，
  并写清"检索为空 ≠ 库是空的"三种读法
- [`docs/FAQ.md`](FAQ.md)：11 个问题型问答（含"会不会把过期的带进上下文""会上传吗"）
- Issue/PR 模板 + `RELEASING.md`：让别人**敢提 issue、知道怎么提**

## F. 度量（别看 star）

| 看什么 | 在哪看 |
|---|---|
| 曝光 | GitHub Insights → Traffic（views / clones / **referrers**） |
| 安装 | PyPI 下载量（发布后）；`pypistats` |
| 真信号 | **第一个外部 issue**、**第一个外部用户跑通**（能贴出 `aml doctor` 输出）、第一个外部 PR |

**30 天目标定"第一个外部用户跑通 + 第一个外部 issue"，不刷 star。**

---

## 我可以立刻做 vs 需要你

| 事项 | 谁做 |
|---|---|
| README 首屏 / QUICKSTART / FAQ / CITATION / Issue·PR 模板 / 本清单 | ✅ 已完成 |
| description + topics（网页点两下，或给 token） | 需要你 |
| PyPI 发行名定夺（建议 `aml-memory`）+ token 或自行发布 | 需要你 |
| MCP 目录提交材料（账号） | 材料我写，提交你来 |
| 三篇问题型文章（中英） | 我写，发布你来 |
