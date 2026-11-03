# 最终验收记录：`v4-single-source-03`

- 记录日期：2026-11-03
- 运行目录：`benchmark/v4/reports/online-closed-loop-react-20261103-01`
- 任务：`v4-single-source-03`
- 后端/模型：`react` / `deepseek-flash`
- 运行模式：在线真实模型调用（已获授权）
- 报告 `run_id`：`c9662fd2-b891-45a9-9440-f391f8efed26`
- 任务 trace `run_id`：`0ab14520-2941-44a1-a832-2b5a7e4e5763`
- 快照/任务集：`cma-v4-658dd583ab18a47f` / `cma-v4-tasks-v1`
- 报告状态：`completed_pending_human_review`
- `TaskSuccessRate`：`null`（未填写未经独立复核的人工评分）

## 1. 运行结果

| 项目 | 实际结果 |
|---|---|
| 模型调用 | 4 |
| 工具调用 | 3 |
| 有效 Claim | 4 |
| 有效 Citation | 4（4 条唯一 Evidence 引用） |
| Gold Evidence 组 | 2 / 2 |
| 必选事实 | 3 / 3 |
| 可选事实 | 1 / 1 |
| `EvidenceRecall@K` | 1.0 |
| `CitationValidity` | 1.0 |
| `FactAccuracy` | 1.0 |
| `OptionalFactAccuracy` | 1.0 |
| `false_refusal_rate` | 1.0 |
| `failure_cases` | `[]` |
| `termination_reason` | `insufficient` |
| `termination_detail` | `finish_validated` |

结论记录：

> **核心答案与引用成功；严格终止状态未达标。**

四条 Claim 均通过 Evidence ID、`content_hash`、Scope、本次运行来源和当前 active 状态校验，
且 Claim 文本是引用 Evidence 原文的逐字子串。两个 Gold Evidence 组均被交付并引用；
三个必选事实和一个可选事实均出现在最终答案中。

但运行终止为 `insufficient`，而冻结 Gold 的
`expected_abstention.expected_termination` 为 `["sufficient"]`，因此不能把本次样本
记录为严格任务成功。该结论不是独立人工 `task_success`，也不替代具名 reviewer 的评分。

## 2. 终止原因边界

`finish` 动作合法，4 条 Claim 也通过校验；`insufficient` 来自 finish 提交的非空
`gaps`。本次记录到 3 条 gap：

1. 日志只记录“索引构建变慢，`index_shards=8`，请求失败率上升”的症状，没有直接写出根因；
   根因来自评审记录和设计文档。
2. 模型声明本次取得的是查询片段，未覆盖日志和 Issue 的完整原文，也未验证当前生产环境。
3. 运行时因 `requires_current_status` 追加“当前有效性未知”。

第 1 条是真实的证据边界；第 3 条在事实上成立，但超出了“历史根因”这一问题的范围。
第 2 条至少对两条 Gold Evidence 是过度谨慎：trace 中 excerpt 覆盖了存储正文全量，
没有截断标记；是否存在更长的父原文，trace 本身不能证明。
因此这是**模型保守回答导致的 `insufficient`**，不是运行时把 `sufficient` 误标为
`insufficient`，也不是 `budget`、`conflict` 或 `tool_failure` 终止。

## 3. 自动引用有效性与语义准确率

必须区分以下两层：

| 层级 | 自动指标能证明什么 | 不能证明什么 |
|---|---|---|
| 引用身份与摘录 | Evidence 存在、Scope 正确、`content_hash` 一致、来自本次运行、当前 active；Claim 文本逐字出现在被引原文中 | Claim 的结论在语义上一定由引用支持 |
| 事实字符串 | 答案正文包含 Gold 结构化事实或 marker group | 该事实是由引用语义推导出来的，而不是碰巧出现 |
| 证据召回 | 必要 Evidence 组被实际交付 | 答案正确使用了这些 Evidence |

因此：

> **`CitationValidity=1.0` 表示引用身份和逐字摘录合法，不等于独立语义准确率；
> `FactAccuracy=1.0` 表示结构化事实命中，也不等于语义支持已自动验证。**

本次语义复核的结论是：核心根因“构建分片过少导致单分片过大”由 `issue_triage`
直接支持，并由设计文档重复；日志支持症状和 `index_shards=8`，但不单独证明根因。
答案本身也明确披露了这一边界。语义支持仍需具名 reviewer 独立判断。

## 4. 评分器修复

本次修复仅涉及 Benchmark V4 评分器：

- `termination_matches_expected` 现在优先读取
  `Gold.expected_abstention.expected_termination`；
- 若该嵌套字段不存在，则兼容旧的顶层 `expected_termination`；
- 未修改冻结 Gold、原始运行结果、历史报告或业务 Agent 的终止规则；
- 新增 `sufficient` / `insufficient` / `conflict` / `budget` 的匹配与不匹配回归测试；
- 新增嵌套字段优先级、顶层兼容和真实 `v4-single-source-03` Gold 路径测试。

原始报告保持原样。报告生成于修复前，因此其 `termination_matches_expected` 仍为
`null`。本次使用临时物化快照和报告中的原始 `raw` 做只读重评，结果为
`termination_matches_expected=false`，同时 `EvidenceRecall@K`、
`CitationValidity`、`FactAccuracy`、`OptionalFactAccuracy` 仍为 1.0。
没有重写报告，也没有重新发送模型请求。

## 5. 验收状态

| 维度 | 状态 |
|---|---|
| API / Runtime | 成功 |
| 检索 | 成功 |
| 引用身份与逐字摘录 | 成功 |
| 必选/可选事实覆盖 | 成功 |
| 核心答案语义支持 | 基本成立，日志仅支持症状 |
| 严格终止状态 | 未达标（`insufficient`，期望 `sufficient`） |
| `TaskSuccessRate` | 仍为 `null`，待独立人工复核 |

最终记录：**部分成功（provisional）——核心答案与引用成功，严格终止状态未达标。**
