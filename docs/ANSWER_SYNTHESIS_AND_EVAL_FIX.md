# 答案综合、Decision 覆盖与输出预算修复

日期：2026-11-01
范围：基于 `third-batch-online-cross-atlas-02` 的人工验收结论，执行范围受限修复。未调用付费模型，未读取/打印 API Key，未修改历史报告，未 Git commit/push。

## 1. 分层答案

`InvestigationResult` 现在保留三层：

1. 原始 Evidence：`EvidenceLedger` 的身份、scope、内容与 hash；`evidence_refs` 保留本轮全部证据引用，完整内容仍可由 EvidenceStore 按 ID/hash 取回。
2. validated Claim：只有通过原文摘录、Evidence ID/hash、refetch、scope 校验的 Claim 才进入 `supported_extract`。
3. 综合回答：`AnswerAgent.build_synthesis()` 只从 validated Claim 确定性组装，不使用模型自由总结。

`【综合回答】` 的生成规则：

- 明确写出用户问题，并只列出可确认内容。
- 每个结论保留全部 Evidence ID/hash。
- 当存在至少两类已验证来源时，新增 `validated_cross_source` 语句，显式关联至少两条 Claim 及其 Evidence 引用。
- 跨来源语句只声明“两段原文均已通过校验、系统不作超出原文的因果或实现断言”，不生成新的因果事实。
- `git_commit` 来源若只有提交信息、没有 `diff --git`/`@@` 等 diff 标记，明确写“仅提交信息；未核验代码 diff”，并说明不能据此确认代码实际实现。
- 提交 SHA 只从 Evidence 内容中的 `commit <sha>` 或已校验的 `original_record_id` 读取，不从题目或标签读取。
- 冲突/缺口存在时，保留已确认部分，并在“未确认部分”列出具体 gap；非独立冲突另生成 `validated_conflict_disclosure` 语句，引用冲突双方原文并声明“系统不择一裁决”。
- `pending_unattributable_conflict` 的 Claim 不进入综合回答，不作为确定事实，只出现在“待核实”审计区段。
- 默认 `【证据】` 只列被输出结论引用的 Evidence ID/hash 与来源元数据，不再倾倒完整 ledger。
- 完整作用域证据保留在 `InvestigationResult.trace[-1].evidence_records`（Evidence ID、hash、scope、原文与来源元数据），同时仍可由 EvidenceStore 按 ID/hash 取回；`evidence_refs` 保留本轮全部引用。
- 可选 `export_jsonl()` 为保护私有正文，只导出 `evidence_records` 的身份/来源字段与 hash，不导出原文；这与“Trace 内保留完整证据”不冲突。

## 2. Decision Memory 覆盖绑定

原问题：问题询问 B，但检索到已审核的 C，`task_coverage` 把 `reviewed_decision` 和 `decision_formation` 都标成 `observed`。

修复位于 `src/investigation_context.py`：

- 新增 `bind_decisions(question, decisions, project_id)`。
- 显式 `dec_...` / `dv_...` ID 命中优先绑定。
- 否则只使用问题与 Decision 标题/正文的非通用主题词重叠；项目名、`采用/方案/决策/状态/提交/代码` 等通用词被排除。
- 至少两个不同主题词重叠才绑定，避免“Atlas”或“方案”单独命中。
- 无法可靠绑定的 Decision 不用于 `decision_formation`、`current_status`、`reviewed_decision` 权威层级或 decision conflict 覆盖。
- `task_coverage.decision_binding` 公开 `bound`、`unbound_version_ids`、`basis`、`matched_terms`。这只表示主题/ID 绑定，不表示语义正确。

因此 `cross-atlas` 中检索到的 C 不再把 B 的决策形成或 reviewed_decision 标成 observed；若只命中证据措辞，仍只是 `observed_unverified`。

## 3. 输出截断

根因不是协议校验，而是 `finish_reason=length` 时 Action JSON 被 reasoning tokens 挤压。修复：

- `Budget.max_truncation_retry_output_tokens` 默认 4096。
- 只在 `protocol_diagnostic` 分类为 `output_truncated` 时，下一次模型调用把输出上限提高到该值；普通 schema/JSON 错误不提高。
- Trace 记录 `requested_output_tokens`、`finish_reason`、`reasoning_tokens`、`recovery_kind="output_budget_retry"`。
- 修正后的 Action 仍必须通过原有 `parse_action` / Pydantic / Evidence 校验，协议没有放宽。
- `run_online.py` 新增 `--max-truncation-retry-output-tokens`，并在计划与配置中记录每次调用上限和总输出 tokens 上界。
- 未做真实付费调用，实际费用仍为 `unavailable_no_pricing`；只提供预算上界。

## 4. 冲突作用域端到端回归

新增测试全部走 `ReActAgent.run_result()`，用确定性的 `_call_llm` 脚本和工具 observation，不直接拼装最终结果：

- 无关结构化冲突 + 独立 Claim：Claim 保留，冲突为 `independent_of_submitted_claims`。
- 直接相关模型冲突 + Claim：Claim 撤回，`termination_reason=conflict`，回答不输出确定 Claim。
- 无法归属冲突 + Claim：Claim 保留但 `pending_unattributable_conflict`，`termination_reason=conflict`，不进入回答正文。
- `conflict-atlas` 同日 `ack_mode` 冲突：回答保留 `exactly_once` 与 `at_least_once` 双方原文，并披露尚未对账。

## 5. 评测权威与版本

详见 `docs/EVALUATION_STANDARD_AUTHORITY.md`。要点：

- 任务级权威是 `benchmark/tasks/labels.json`。
- `benchmark/labels/gold.json` 是检索/Gold 基准，其 `expected_termination` 不是任务级标准，且 `cross-atlas` 条目仍为 `pending_human_review`。
- 未修改任何标签。
- 新增 `scoring_method_version=cma-task-eval-method-v2` 与 `evaluation_authority` 元数据；新分数不得与旧方法版本直接比较。

## 6. 验证

```powershell
python -m unittest discover -s tests
```

结果：`Ran 212 tests ... OK`。

新增/更新覆盖：综合回答与跨来源关联、commit-only 披露、未引用证据不进默认回答、Decision 主题/ID 绑定、无关 Decision 不影响覆盖、输出预算单次重试、普通协议错误不提高预算、三类冲突作用域端到端、`conflict-atlas` 双方值披露。

## 7. 后续 3 个在线验收场景命令

以下命令仅在操作者显式授权并已配置模型凭据后运行；每个 `--output` 必须是尚不存在的新目录。命令不读取或输出 API Key；`--approved-origin` 必须与当前 provider 配置的 origin 完全一致（若使用 SiliconFlow，替换为 `https://api.siliconflow.cn`）。

```powershell
# 1) 冲突分支：必须实际检索到同日两份 ack_mode 记录，未产生冲突不算通过
python -m benchmark.tasks.run_online `
  --online `
  --approved-origin https://api.deepseek.com `
  --task conflict-atlas `
  --output benchmark/reports/third-batch-online-conflict-atlas-20261101-01 `
  --max-model-rounds 5 `
  --max-tool-calls 8 `
  --max-output-tokens 2048 `
  --max-truncation-retry-output-tokens 4096 `
  --deadline-seconds 120

# 2) 跨来源：验证综合回答同时关联讨论与提交，commit-only 时带未核验 diff 声明
python -m benchmark.tasks.run_online `
  --online `
  --approved-origin https://api.deepseek.com `
  --task cross-atlas `
  --output benchmark/reports/third-batch-online-cross-atlas-20261101-03 `
  --max-model-rounds 5 `
  --max-tool-calls 8 `
  --max-output-tokens 2048 `
  --max-truncation-retry-output-tokens 4096 `
  --deadline-seconds 120

# 3) 决策覆盖：检索到的无关 Decision C 不得把所问 B 标为 observed
python -m benchmark.tasks.run_online `
  --online `
  --approved-origin https://api.deepseek.com `
  --task decision-atlas `
  --output benchmark/reports/third-batch-online-decision-atlas-20261101-01 `
  --max-model-rounds 5 `
  --max-tool-calls 8 `
  --max-output-tokens 2048 `
  --max-truncation-retry-output-tokens 4096 `
  --deadline-seconds 120
```

## 8. 尚未验证

- 未运行真实 DeepSeek/SiliconFlow 模型；无法确认模型在 `--max-truncation-retry-output-tokens 4096` 下实际不再截断。
- 未验证真实在线 `conflict-atlas` 是否一定检索到两份同日证据；若没有，冲突分支不算通过。
- 未验证语义冲突、跨日冲突、自由文本冲突的自动归属；当前仍保守标记 `unattributable` 或无法检测。
- 未做人工 `TaskSuccessRate` 评分；`TaskFactCoverage` 仍不是语义成功率。
- 未验证 commit SHA 在真实模型提交的 Claim 中是否总被原文覆盖；实现只从已校验 Evidence 读取。
