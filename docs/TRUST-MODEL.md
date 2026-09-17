# 信任模型（Trust Model）

> 这份文档回答一个问题：**库里这些东西，凭什么值得信、谁有权写、检索时给不给它位置。**
>
> 起因：一次外部 review 指出"技能内容被镜像进知识库后可能被语义检索自动召回"——
> 那是**程序性指令**，不应该和普通知识共享同一个召回入口。顺着这条线把整层的信任边界写清楚。

## 一、六种东西，别混在一起

| 类型 | 是什么 | 打标 | 谁能写 | 检索里怎么对待 |
|---|---|---|---|---|
| **observation** | 会话原文（一条任务、一条回复） | `kind:task` / `kind:reply` + `agent:` + `session:` | 采集器自动 | 只在"全库"兜底层出现，噪声最大 |
| **memory** | 项目事实（进度、客户细节、临时结论） | `project:<名>` | 采集器 / agent 自动 | 第二层（本项目历史） |
| **knowledge** | **跨项目可复用**经验（方法论、坑、工具用法、决策理由） | `kind:knowledge` + `reusable:true` + `domain:*` + `ktype:*` | 蒸馏自动（带复核期）/ 人 | 第一层（沉淀），最优先 |
| **procedure** | 程序性知识：**照做会改变行为**的步骤（含技能正文） | `kind:procedure` | 镜像/导入自动，**默认不进检索** | **默认跳过**，需显式要求 |
| **skill** | 可被显式加载的技能（`SKILL.md` + 元数据） | 技能目录本身 + `patrol` 元数据 | 人安装 / patrol 更新 | 只经**显式加载**，不经检索 |
| **policy** | 规则、红线、必须遵守的约定 | `kind:policy`（规划中） | **仅人工/管理** | 最高优先级，注入时置顶 |

关键区分：**"知道"和"照做"是两种权限。** 一条知识被召回，最坏结果是给出错误信息；
一条指令被召回，最坏结果是 agent 直接改了别的东西。

## 二、信任阶梯

```
原始会话 observation        LOW      —— 可能只是我随口一说
   ↓ 蒸馏（LLM 提炼 + 复核期）
记忆 memory                 MEDIUM   —— 项目事实，会过期
   ↓ 跨项目抽象 + 复核
知识 knowledge              MEDIUM+  —— 有来源、有复核期、有置信度
   ↓ 人工/复核确认
程序 procedure              HIGH     —— 照做会改行为，必须显式取用
   ↓ 安装 + 审计
技能 skill                  VERY HIGH—— 有版本、有来源、有暂存/回滚
   ↓ 人工批准
策略 policy                 HIGHEST  —— 红线，不许自动生成
```

**越往下，证据要求越严、自动写入越不允许。**

## 三、权限通道（authority lanes）

| 写入者 | 能写哪层 | 不能写哪层 |
|---|---|---|
| 采集器（`aml sync` / `watch`） | observation | 其它全部 |
| 蒸馏（`aml distill`） | memory（project 标签）、knowledge（自动 + 复核期） | procedure / skill / policy |
| agent 通过 MCP `store` | memory（给了 `project`）、knowledge（无 `project`） | procedure / skill / policy |
| 镜像（`patrol sync`） | procedure（技能正文副本，**默认不进检索**） | knowledge |
| patrol 更新 | skill（三条安全闸门：有本地改动只暂存） | policy |
| 人 | 全部 | — |

现状与缺口（诚实标注）：

- ✅ **已实现**：`store` 强制分层（给 `project` 就只打 `project:`）；蒸馏产物带 `review_after`；
  技能更新有"本地改动永不覆盖"的闸门。
- ⚠️ **部分实现**：技能正文镜像进向量库时打的是 `kb:技能原始`，**没有** `kind:*` 标签 ——
  所以它不会进沉淀层优先位，但仍会在"全库"兜底层被召回。
- ❌ **未实现**：authority / freshness 乘进排序；policy 层；procedure 的默认排除；
  outcome 反馈（`usage_count` / `success_count`）。

## 四、今天就能守住的四条（不依赖新功能）

1. **别把技能当知识用**：想让 agent 用某个技能，**显式加载**它；不要指望"检索恰好把它捞出来"。
2. **写回时先问一句"它对别的项目成立吗"**：成立 → `kind:knowledge` + `domain:*`；不成立 → `project:<名>`。
   两样都不占的，宁可不写。
3. **复核期不是装饰**：`review_after` 到了就去 `aml review` 看一遍 —— 技术类结论（限流窗口、
   站点可用性、工具行为）180 天就该怀疑了。
4. **任何自动动作都要能回滚**：技能覆盖有 `_backup/`，合并有快照，库有 `restore`。
   没有回滚路径的自动化不要开。

## 五、下一步（按依赖排序）

1. 入库技能正文时打 `kind:procedure`；检索默认跳过，`--include-procedure` / 显式 tag 才返回
2. 排序加入 `authority`（谁写的）与 `freshness`（多久没验证），并在输出前缀显示
3. MCP `store` 增加"这是知识候选，需要复核"的标记路径，而不是直接落 `kind:knowledge`
4. `provenance` 补 `derived_by`（模型 + prompt 版本）与 `source_turns`
5. outcome 反馈字段落地，让"被召回"与"有用"分开计分
6. `supersedes / contradicts / deprecated_by` 显式关系（现在只有"以时间较新为准"的合并口径）

完整清单见 [`ROADMAP.md`](../ROADMAP.md) 的 Step 3。
