# 5 分钟跑通（Quickstart）

目标：**10 分钟内完成第一次成功召回** —— 装 CLI → 指到你的 agent 历史 → 检索到一条真东西。

三层东西，先分清楚（后面排障全靠它）：

```
各 agent 的会话文件        aml（本项目）           向量后端
~/.dsh/sessions/*.zstd  →  采集/蒸馏/检索/治理  →  mcp-memory-service（HTTP+MCP）
~/.claude/projects/**                        SQLite + sqlite-vec + 本地嵌入
```

`aml` 只管第一层到第二层；**向量存储与检索后端是独立服务**（本机跑，不上云）。

---

## 1. 装 CLI（不需要管理员）

```bash
uv tool install "git+https://github.com/kh859278/agent-memory-layer.git"   # 或
pipx install "git+https://github.com/kh859278/agent-memory-layer.git"      # 或
pip install "git+https://github.com/kh859278/agent-memory-layer.git"
```

Windows 一键脚本（幂等、不动系统目录）：

```powershell
irm https://raw.githubusercontent.com/kh859278/agent-memory-layer/main/install.ps1 -OutFile install.ps1
notepad install.ps1        # 想先看内容就先看一眼
.\install.ps1 -DryRun      # 只看会执行什么
```

> PyPI 上还没有这个包（分发名被别的项目占了），所以上面都是**从 git 装**。

## 2. 建数据目录

```bash
aml init
```

它建 `$AML_HOME`（默认 `~/aml`）下的骨架 + `config.yaml`，并**探测本机能看到的 agent 会话**
（DSH / Claude Code / Kimi Code）。想在别的盘存数据就设环境变量：

```bash
export AML_HOME=/path/to/data        # PowerShell: $env:AML_HOME="D:\aml"
```

## 3. 起后端（自己管也可以）

按 [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) 的说明让它监听
`127.0.0.1:8000`。然后在 `$AML_HOME/config.yaml` 里对齐两项：

```yaml
memory_api: http://127.0.0.1:8000
db_path: /path/to/mcp-memory/sqlite_vec.db   # 只有直连操作（时间回填等）才需要
```

## 4. 体检

```bash
aml doctor
```

输出是**逐项带修法**的清单：目录 / 服务 / 向量覆盖 / FTS / 索引新鲜度 / 各适配器能不能看到会话 /
运行数据有没有被删。**任何一项 ❌ 先修它**，别急着往下走。

## 5. 第一次入库 + 第一次召回

```bash
aml sync            # 采集所有 agent 会话入库（写入时回填原始时间戳）
aml sync --dry-run  # 只想先看会采到什么

aml search "上次那个编码问题是怎么解决的" --phase P2
```

`search` 的三种结果都要能读懂：

| 结果 | 含义 |
|---|---|
| 有命中 | 直接看每条前缀：`[沉淀/本项目/全库 | domain | 日期 | 相似度]` |
| 空 + "本次没有真的去查" | 同一阶段同一查询 10 分钟内不重查（防重复）。换措辞或加 `--allow-repeat` |
| 空 + "语义返回 N 条，最高分 X" | **真的没命中**：换措辞、放宽 `-n`、或用 `--tag domain:xxx` 收窄 |

想以后不用手动跑：`aml watch`（归档即时入库 / 会话静默 ≥120s 增量入库）。

## 下一步（按需）

```bash
aml ingest-kb          # 把知识库文档也灌进检索
aml distill --list     # 把会话蒸馏成跨项目知识（需要 LLM key）
aml patrol check       # 看纳管的技能有没有上游更新（不动文件）
aml bench --tasks …    # 检索层基准：入口命中率 / P@3 / 注入量
aml mcp                # 起 MCP server，把记忆层接给任何 MCP 客户端
```

## 卡住了？

1. `aml doctor` 先看哪项红。
2. `aml search "任意泛词" --explain` 看诊断（每层阈值、候选数、最高分）。
3. 检索为空 ≠ 库是空的：看它给的"为什么空"那一行，按提示换招。
