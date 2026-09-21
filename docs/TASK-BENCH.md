# 任务级基准（`aml bench --task-level`）

> 这一页回答的问题是：**这套记忆层到底有没有让 agent "把事做成"？**
> 检索基准（`aml bench`）只证明"入口召回得住、上下文不贵"；那是必要不充分条件。
> 任务级基准是**真起一个 agent 去干活**，用可判定的验收命令判成败。

## 一、两层基准的分工（别混着看）

| | 检索层 `aml bench` | 任务层 `aml bench --task-level` |
|---|---|---|
| 量什么 | 该被想起来的经验有没有召回、注入多少字 | 任务做没做成、返工几轮、多久、多少 token、有没有被记忆带偏、有没有踩坏既有功能 |
| 花不花钱 | 不花 | **花**（真跑 agent） |
| 可复现性 | 完全可复现（同库同表 → 同数字） | 同模型同任务基本稳定，但不是逐字可复现 |
| 用途 | 回归门禁（`--min-hit-rate`） | "有没有把事弄坏"的守门 + ON/OFF 差值 |

## 二、六个指标（都能客观算出来，不靠感觉）

| 指标 | 定义 | 为什么是它 |
|---|---|---|
| `success` | 任务自带验收命令退出码 0，且 `expect` 都在、`forbidden` 都不在 | 唯一的硬结论 |
| `rework` | agent 自报轮数 `num_turns` + 改动文件数 | 返工/反复试错的代理量 |
| `time` | 墙钟耗时 + agent 自报耗时 | 到解时间 |
| `tokens` / `cost` | 输入/输出 token 与美元成本 | 记忆不是免费的：注入会推高输入 token |
| `forbidden` | 任务声明的**过期/错误做法**有没有出现在产出里 | 近似的"被记忆带偏率" |
| `regression` | 起跑前能过的检查，跑完挂了 | "踩坏既有功能"率（技能回退的同一口径） |

报告里最后给的是 **ON − OFF 的差值**，不是绝对值：绝对值会被模型能力与任务难度带偏，
两组用的是同一个 agent、同一批任务、同一台机器，差值才归因得到"记忆的作用"。

## 三、怎么跑

```bash
# 1) 写任务表：$AML_HOME/state/bench-tasks-task.jsonl（不进仓库，里面有你的项目细节）
cp tools/bench/task-level.example.jsonl "$AML_HOME/state/bench-tasks-task.jsonl"

# 2) 跑（默认 off,on 两组；默认 agent 是 claude -p）
aml bench --task-level --repeats 1

# 3) 常看的开关
aml bench --task-level --arms on              # 只跑有记忆那组
aml bench --task-level --keep                 # 保留临时工作目录，出问题进去看
aml bench --task-level --feedback             # 把结果自动回写成记忆反馈（会改数据）
aml bench --task-level --agent "claude -p --output-format json --no-session-persistence"
```

退出码：有 `regression` 或 `forbidden` 命中时非 0，可以直接挂进 CI 当"没把事弄坏"的门禁。
报告落在 `$AML_HOME/state/bench/task-runs/task-bench-<时间戳>.json`（注入的记忆**正文不落盘**，只留 hash 与字数）。

### 任务表格式

```json
{"id": "py39-newline", "phase": "P2", "query": "python 3.9 write_text newline 不支持",
 "prompt": "……给 agent 的任务描述……",
 "fixture": "add-func",
 "verify": "python -m pytest -q",
 "expect": ["passed"],
 "forbidden": ["write_text(newline"],
 "expect_memory": ["write_lf"],
 "regression": "python -m pytest -q",
 "timeout": 600}
```

| 字段 | 说明 |
|---|---|
| `prompt` | 必填。给 agent 的任务描述 |
| `query` | memory ON 组的检索词（省了就退化成用 prompt 检索） |
| `fixture` | `tools/bench/fixtures/` 下的目录名（或绝对路径）：会被**拷进一次性工作目录** |
| `verify` | 验收命令，退出码 0 = 通过。**判分前**才把 fixture 的 `_hidden/` 拷进来 |
| `expect` / `forbidden` | 必须出现 / 绝不出现的子串（查 agent 产出、验收输出、工作区正文） |
| `expect_memory` | 出现了就说明"真用了注入的经验"（给 `memory_used` 指标用） |
| `regression` | 起跑前先在干净副本上跑一遍，跑完再跑一遍；只有"前过后悔"才算账 |
| `timeout` | 单次 agent 运行上限（秒） |

### agent 契约

命令从 **stdin 读 prompt**，往 stdout 打**一行 JSON**（`claude -p --output-format json` 的格式即可）：

```json
{"result": "...", "num_turns": 3, "duration_ms": 12000, "total_cost_usd": 0.12,
 "usage": {"input_tokens": 23000, "output_tokens": 400}, "is_error": false}
```

少字段按"未知"处理，不因此判失败；但**没有可解析的 JSON 就算失败**（否则"什么都没说"会被算成成功）。
换 agent：`--agent` 或环境变量 `AML_TASK_BENCH_AGENT`。

## 四、`_hidden/`：唯一能区分"记得"和"猜得到"的手段

fixture 里的 `_hidden/` 目录**默认不给 agent**，判分前才拷进工作区。于是：

- 验收标准若写在仓库里，两组都能读到 → 量的是"能不能读题"，不是记忆；
- 验收标准在 `_hidden/` 里，OFF 组只能靠猜，ON 组要靠**记忆层里那条跨会话经验**。

`tools/bench/fixtures/gbk-entry` 就是这个套路：`app.py` 在 GBK 控制台下会
`UnicodeEncodeError`，验收标准（用 `PYTHONIOENCODING=gbk` 跑一遍）藏在 `_hidden/` 里。
"Windows 上打印中文/emoji 会崩、入口处要强制 UTF-8 stdio"这类坑，**仓库里没有、环境里真实存在** ——
这正是记忆层该值钱的地方。

## 六、首次实测（2026-09-17，本机 · claude 2.1.220）

3 个仓库自带 fixture 任务 × ON/OFF，共 6 次运行，**总花费 $1.79**：

| 分组 | 成功率 | 轮数 | 改动文件 | 耗时 | token | 成本 | 违禁 | 回归 | 注入字 |
|---|---|---|---|---|---|---|---|---|---|
| OFF | 100% | 8 | 1 | 38.7s | 29 088 | $0.9166 | 0 | 0 | 0 |
| ON | 100% | 8 | 1 | 54.5s | 28 972 | $0.8737 | 0 | 0 | 560 |

差值（ON − OFF）：成功率 **+0%**｜轮数 −0.3｜token −116｜耗时 +15.8s｜违禁 +0｜回归 +0。
ON 组 3 次里有 2 次出现"确实用了注入经验"的痕迹。

**怎么读这个结果（别自欺）**：

1. **在这批任务上，记忆层没有改变成败** —— 三个任务的做法都能从仓库/prompt 里推出来
   （`_hidden/` 的 GBK 验收标准也一样：prompt 已经点明了"GBK 控制台崩溃"，
   一个能干活的 agent 直接猜中 `reconfigure(encoding="utf-8")`）。
   这正是"两组都可能猜对"的诚实版本：**记忆只在"仓库里没有、prompt 里也不说"的约定上才值钱**。
2. token 与成本没有变差（注入 560 字 ≈ 几百 token，被 agent 自己的探索波动淹没了）。
3. 耗时 ON 组更长（+15.8s），样本太小，**不当结论**（可能是注入内容引发的额外阅读）。
4. 第一批跑出来的两个"失败"其实都是**基准自己的 bug**（`_hidden` 定位写错、
   判分输出按 UTF-8 解码 GBK 输出），已修并加了回归测试 ——
   这也说明基准代码和被测代码一样需要回归测试，否则会拿假失败当真结论。

**下一批任务该往哪儿找**：把"只存在于记忆里的约定"做成任务
（本仓库约定用 `text.write_lf()` 而不是 3.9 不支持的 `Path.write_text(newline=...)`；
`repo_guard` 提交必须显式列路径而不能 `git add -A`……），
prompt 只给现象、不点明原因，验收标准放 `_hidden/`。

## 七、反事实臂（`--ablate N`）与注入量分位数

**反事实臂**回答一个更狠的问题：**注入的记忆到底有没有被用上？**
`--ablate 1` 在 ON 组之外再跑一组"检索照做、但把排在最前的 1 条记忆藏掉"，
报告给出 `反事实（藏掉前 N 条 − 完整注入）` 的成功率/轮数/token 差：
差值都接近 0 → 那些记忆可能只是装饰。这是把"记忆有用"从主张变成可测的最短路径
（成本：多一组运行）。

**注入量分位数**：平均值会掩盖尾部，真实成本要看 P50/P95/max。

### 检索层：问法对照首次实测（2026-09-21，3 个示例任务，只读）

| 问法 | 命中 | 平均注入 | P50 / P95 / max | P@3 | R@3 |
|---|---|---|---|---|---|
| `query`（知识标题式，偏易） | 2/2 | 298 字 | 298 / 587 / 587 | **33%** | 100% |
| `alt_query`（人会这么问） | 2/2 | 614 字 | 339 / 889 / 889 | **83%** | 100% |

读法：命中率两者都 100%（所以单看"命中率"分辨不出问题），但 **P@3 从 33% 到 83%** ——
标题式问法召回的前 3 条里有 2 条是无关的，平均注入也只有真实问法的一半。
这正是"平均值 + 单一命中率"会掩盖的东西：**入口不是没召回，是召回里掺了废的**。

## 八、自动反馈（`--feedback`）

跑完把结果回写成记忆反馈（`worked / failed / used`），归因口径刻意保守：

| 情况 | 记什么 |
|---|---|
| 这条记忆**自己的正文**里出现了被 agent 真用上的做法（`expect_memory` 命中）+ 任务成功 | `worked` |
| 这条记忆**自己的正文**里出现了任务声明的过期/错误做法（`forbidden` 命中） | `failed` |
| 其余被注入的 | `used`（被用过，**不主张**它有用） |

不搞连坐：任务失败不会把所有注入的记忆都记成 `failed`。
所以任务表里的 `forbidden` / `expect_memory` 最好**直接摘那条记忆正文里的词**，归因才精确。

## 九、诚实边界（写在报告结尾，也写在这里）

1. memory OFF 组会**显式禁用历史检索**（prompt 里加一句限制），否则"对照"根本不成立；
   代价是两组的 prompt 不完全对称 —— 这是为了有效性必须付的。
2. agent 带着自己的通用知识 + 本机配置，两组都可能"猜对"，所以**只看两组之差**，不看绝对值。
3. 一次 run 只有 n 个任务：**不是统计显著性证明**，是"有没有把事弄坏"的守门指标。
4. 跑基准要花钱（每次运行动辄上万输入 token），报告里逐行写出成本。
5. 基准用 `--no-session-persistence` 起 agent，**避免把基准跑出来的会话灌进记忆层**污染数据。
