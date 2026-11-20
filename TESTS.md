# TESTS — agent-memory-layer

> 口径按 `~/.dsh/AGENTS.md` §12：一个**受影响的模块/功能点** = 一个测点，每点一条可复跑判据。
> 最近一次全量：**2026-11-20 20:0x，416 passed**（`python -m pytest -q`）。

## 本批改动（2026-11-20）：让记忆层"只记该记的"+"可解释"

| # | 测点 | 判据（可复跑） | 最近结果 |
|---|---|---|---|
| 1 | 采集过滤：样板文本不入库 | `python -m pytest tests/test_boilerplate.py -q` | 7 passed |
| 2 | 存量降噪：分类 + 软删 + 可回滚 | `aml denoise`（预演）→ `aml denoise --apply` → 再跑预演应为 0 | 首轮 8,295（boilerplate 8,268 / short 25 / ui_echo 2）；后续补清 10 / 67 / 335（旧格式 `[SCHEDULE REMINDER]`）；最终预演 **0**，活跃 29,678 → **21,055** |
| 3 | 软删对关键词兜底生效 | `python -m pytest tests/test_boilerplate.py -q -k soft_deleted` | passed（FTS/LIKE 两条路径都不再返回已删行） |
| 4 | doctor 覆盖率口径 | `aml doctor` | `向量覆盖：21383/21383（100.0%）（另有 9,283 条软删不计数）` |
| 5 | 可解释：`aml why` | `aml why 8fc16bd4` | 打印来源 `knowledge-distill`、领域 `env-windows`、复核期、被召回 4 次 |
| 6 | 可解释：检索带 hash | `aml search --phase P2 --trace "powershell 编码 乱码"` | 每条末尾 `[h:fefebe94]` 形式 |
| 7 | 全量回归 | `python -m pytest -q` | 416 passed |
| 8 | 检索基准基线 | `aml bench --tasks state/bench-tasks-kb.jsonl --forms query,alt --json` | query：命中 19/20、P@3 **0.592**、R@3 0.95、均注入 726 字；alt：命中 19/20、P@3 **0.5**、R@3 1.0、均 653 字。补清 335 条旧格式样板后复跑**同值**（稳定） |

## 基线文件与用法

- 任务表：`state/bench-tasks-kb.jsonl`（20 题，标题式 + 人间问法；**不进仓库**，属运行数据）
- 基线报告：`state/bench/baseline-kb-20261120.json`
- 回归门禁：`aml bench --tasks state/bench-tasks-kb.jsonl --forms query,alt --min-hit-rate 0.9`
  掉到 0.9 以下退出码非 0；P@3 目前**只能看趋势**，还没有自动门禁。

## 未覆盖 / 待补

- **公开基准**（LongMemEval / HaluMem）尚未接入：它们自带对话 haystack，接进来要先灌库，属下一批。
- **冲突检测**：后端 `memory_conflicts` 仍把近重复当矛盾（9,111 对），本批未动；aml 不消费它。
- **截断策略**：P2 命中经常顶满 600 字预算、最后一条断在半句（本批未改）。
