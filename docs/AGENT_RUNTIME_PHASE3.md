# Phase 3：上下文预算、证据选择、压缩与可观测性

Phase 3 在现有 ReAct / EvidenceLedger / Claim 校验 / Decision Memory 之上增加确定性的
Context Engineering。它只改变**传给模型的上下文视图**，不改变检索策略、证据事实、
Run/Session 生命周期或事件 Schema 的既有语义。

## 1. 模块边界

| 模块 | 文件 | 职责 |
|---|---|---|
| Token 预算与 tokenizer 适配 | `src/context_budget.py` | 模型窗口、输出预留、安全余量、分类 token 估算 |
| 结构化调查上下文 | `src/investigation_context.py` | 版本化状态、证据选择、去重、分层压缩 |
| ReAct 集成 | `src/agents/react_agent.py` | 每轮构造 request、预算检查、Trace/Event |
| Run 事件与查询 | `src/runtime/store.py`、`src/api/runtime_routes.py` | `context_selected` / `context_limit` 持久化与查询 |

原有 `max_context_bytes` 保留为辅助硬上限；新增 token 上限取
`min(model_context_window - reserved_output_tokens - safety_margin,
     remaining_total_tokens)`。两者同时生效。

## 2. Token 策略

- `deepseek-chat` 默认窗口 64,000；`gpt-4o` 128,000；Claude 系列 200,000；
  未识别模型回退 64,000，实际解析值写入 Trace。
- 默认 `reserved_output_tokens = min(Budget.max_output_tokens, window/2)`，
  默认 `safety_margin_tokens = 512`。
- 默认使用保守估算：ASCII 3.2 字符/token、CJK 1 字符/token、其他 2 字符/token，
  向上取整，并按消息加 4 token。**估算值不是计费 token**。
- 只有显式请求 `context_tokenizer="tiktoken"` 且依赖可导入时才使用真实 tokenizer；
  回退原因记录为 `tiktoken_unavailable` / `encoding_unavailable`。
- `Budget` 新增字段：
  `context_window_tokens`、`context_safety_margin_tokens`、`context_tokenizer`、
  `context_max_excerpts`、`context_max_excerpts_per_evidence`。
- 这些字段随 Run 的 `budget_json` 持久化，Trace 同时记录
  `budget_policy_version = phase3.budget.v1` 和实际 tokenizer 模式。

## 3. 结构化调查上下文

`InvestigationContext.request()` 生成第三类消息（`role=user` 的 JSON 状态），
包含：

- `context_schema_version = phase3.investigation.v1`
- `task`：目标、scope、请求目标、必需来源类型、只使用本次证据的指令
- `evidence_index`：每条 Evidence 的身份、locator、时间状态、摘录 locator、
  `selection` 与 `selection_reason`
- `retained_excerpts`：本轮实际展示的已验证片段（最多一个 body/证据）
- `verified_claims`：仅本次运行校验通过的 Claim，标记 `validated_claim_from_this_run`
- `gaps` + `gap_status="unresolved"`、`conflicts`
- `tool_history`、`tool_failures`、`attempted_empty_searches`、`completed_steps`
- `decision_memories`（仅作为线索，`source_kind` 标明不是原始证据）
- `task_coverage`（仅非空核心字段）
- `session_context_meta`：历史调查数量、已验证引用数量、`fact_source=false`

历史回答、模型摘要和会话上下文都不会变成已验证事实；Claim 只能引用本次
Ledger 中可回取且 hash 一致的 Evidence。

## 4. 证据选择与去重

选择顺序是确定性的，检索相似度只用于同优先级内排序：

1. 关键冲突证据
2. 已验证 Claim 的支持证据
3. 问题中显式引用的 Evidence ID / locator
4. 任务覆盖目标支持证据
5. 问题相关性
6. 仅保留索引

规则：

- 同一 Evidence 的多个 chunk 先按身份分组，只保留一个候选；
- 相同来源文本只保留一个 body，其余标记 `duplicate_source_text`；
- 每证据/每请求的摘录数量受 `context_max_excerpts` 与
  `context_max_excerpts_per_evidence` 限制；
- 冲突与 Claim 证据的身份优先保留，body 为尽力保留，不会被低优先级证据挤掉；
- 丢弃只影响展示 body；完整 Evidence 始终保留在 Ledger 和 `evidence_index`；
- 每个 retained/index_only/dropped 决策写入 `evidence_decisions`，包含原因、
  估算 token、`excerpt_basis` 和 locator；
- `Excerpt.verify()` 继续校验父 Evidence 的 identity/scope/range/lines；
  `parent_evidence_fallback` 同样回退到父 Evidence 校验。

## 5. 分层压缩

按固定优先级执行，系统规则、scope、任务目标、工具失败和冲突身份永不被删除：

1. 压缩会话历史（保留原始已验证引用、缺口/冲突数量）；
2. 移除工具观察中的可选元数据；
3. 压缩工具历史与已完成步骤；
4. 压缩 Decision Memory 线索；
5. 压缩 coverage 细节；
6. 按优先级逐个省略摘录 body（冲突/Claim 最后）。

如果强制内容仍超 token 或字节上限，返回结构化 `ContextLimitError`：

```json
{
  "code": "context_limit",
  "context_limit": {
    "reason": "mandatory_state_exceeds_budget",
    "category": "mandatory_state",
    "required_input_tokens": 1234,
    "available_input_tokens": 512
  },
  "context_schema_version": "phase3.investigation.v1",
  "budget_policy_version": "phase3.budget.v1",
  "tokenizer_mode": "estimated"
}
```

ReAct 将其转换为 `termination_reason=budget`、
`termination_detail=context_token_budget`（或 `context_byte_budget`），
并在调用模型前停止。

## 6. Session Context 协调

- 会话上下文仅用于理解追问，带显式“不是事实来源”标记；
- 会话历史与当前调查共享同一个 token 上限，默认最多占 12%；
- 超出时先压缩最旧调查，保留原始 EvidenceRefs（最多 5 条）、缺口/冲突计数；
- 当前运行的 EvidenceLedger 永远优先，历史 `verified_evidence_refs` 只作为引用提示；
- 本阶段不引入跨 Session 自动语义记忆。

## 7. Trace 与 Run 查询

每轮在 `model_requested` 之前写入 `context_selected`，溢出时写入
`context_limit`。Payload 只记录统计、标识和原因，不记录完整 prompt、原始
Evidence body 或 API Key。

主要字段：`context_schema_version`、`budget_policy_version`、
`model_context_window`、`reserved_output_tokens`、`safety_margin_tokens`、
`estimated_input_tokens`、`tokenizer_mode`、`category_tokens`、
`candidate_evidence_count`、`selected_evidence_count`、
`deduped_evidence_count`、`pruned_count`、`pruning_reasons`、
`important_conflicts_retained`、`gap_count`、`context_limit`。

查询示例：

```powershell
curl "http://127.0.0.1:8000/runs/<run_id>/context-trace?project_id=<project>"
curl "http://127.0.0.1:8000/runs/<run_id>/events?project_id=<project>"
curl "http://127.0.0.1:8000/runs/<run_id>/result?project_id=<project>"
```

`GET /runs/{run_id}/context-trace` 返回
`context_trace`（原始事件）、`rounds`（payload 列表）、`run_status` 和
单位说明。`InvestigationResult.context_trace` 同时保留每轮统计，便于离线回放。

## 8. 验证与 Benchmark

新增测试 `tests/test_context_engineering.py` 覆盖：

- 估算模式标注与真实 tokenizer 回退；
- 强制状态溢出结构；
- 低分关键冲突保留；
- Claim 支持证据保留；
- 重复 chunk/文本去重且 Ledger 不变；
- 会话历史有界、非事实、不能挤占当前证据；
- ReAct 最终请求确实包含版本化状态、工具失败、空检索和已完成步骤；
- Trace 持久化与 Run API 查询；
- 固定输入可复现。

独立 benchmark 位于 [benchmark/v3](../benchmark/v3/README.md)，不修改 V1/V2
冻结产物。报告只比较确定性上下文构造，不声称真实 LLM 任务成功率。

## 9. 已知限制

- 默认 token 计数是保守估算，不是 provider 计费值；
- 未实现模型驱动的自动摘要，压缩规则全部确定性；
- 语义正确性与任务成功率没有在本地脚本 benchmark 中测量；
- 冲突自动裁决仍不在范围内，冲突只保留并披露；
- tokenizer 的跨模型精确性依赖可选 `tiktoken` 及模型编码可用性。
