# P0：冲突影响范围修复与 Claim 可观测性

日期：2026-11-01
范围：仅修复“无关冲突导致整题无法回答”，保留严格 Evidence 身份校验和冲突披露。未调用真实模型，未读取/输出 API Key，未修改历史报告，未 Git commit/push。

## 1. 根因

历史 `cross-atlas` 运行中，检索与引用均成功（EvidenceRecall 2/2、CitationValidity 9/9），但最终 `claims=[]`。直接原因在：

- `src/agents/answer_agent.py` 的 `compose_investigation()`：
  - 只要 `conflicts` 非空，就执行 `accepted = []`，把所有已通过原文摘录、Evidence ID/hash、scope、refetch 校验的 claim 一次性清空。
- `render_investigation()`：
  - 使用 `if claims and not conflicts`，因此任何冲突都会让回答退化为“证据不足 + 来源片段”。
- `src/agents/react_agent.py`：
  - 只要存在任意冲突就设置 `termination_reason="conflict"`，不区分冲突是否影响当前 claim。
- `benchmark/tasks/scoring.py`：
  - `_fact_score()` 对整段渲染后的 answer 做关键词扫描，把【证据】【未解决的缺口】【冲突】中的事实词也算作回答事实覆盖，导致 `TaskFactCoverage=1.0` 的假阳性。

此外，`InvestigationResult` 只保留最终 `claims`，没有记录提交、校验丢弃、冲突丢弃和带未解决冲突保留的运行时数量，无法区分“模型没提 claim”和“提了但被全局清空”。

## 2. 修复机制

### 2.1 冲突作用域

`Conflict` 增加运行时归属信息（模型输入不可信，compose 阶段会重建/覆盖）：

- `origin`: `evidence_version` / `structured_assertion` / `model_reported`
- `fields`: 结构化断言字段（如 `ack_mode`）
- `subject`: 两份 Evidence 明确共享的 subject/component/service/feature/topic
- `relation`: `affects_submitted_claim` / `independent_of_submitted_claims` / `unattributable`
- `affected_claim_indexes`: 受影响的已提交 claim 下标

`AnswerAgent._conflict_relation()` 只依据可验证关系判断：

1. Claim 与冲突的 `(evidence_id, content_hash)` 有交集 → 直接影响。
2. 冲突有显式结构化字段：
   - 字段出现在 claim 原文，或与 claim 引用 Evidence 的 `metadata.assertions` 字段相交 → 影响；
   - 若冲突有 subject 且 claim/证据上下文不包含该 subject → 判为无直接关联；
   - claim 引用 Evidence 有显式 assertion 字段、且与冲突字段完全不相交 → 判为无直接关联；
   - 以上都不成立 → `unattributable`。
3. 无字段但有显式 subject：subject 匹配 → 影响；不匹配 → 无直接关联。
4. 无 Evidence ID/hash 交集、无字段、无 subject → `unattributable`，保守保留但标记待核实。

处理结果：

- 受直接影响的 claim 不进入最终 `claims`，计入 `claims_discarded_by_conflict`；冲突在回答中披露，不择一裁决。
- 无直接关联的 claim 正常保留；无直接关联的冲突放入【附录】并保留在 Trace/`conflicts` 中。
- 无法归属的冲突：claim 仍保留，但 `verification_status="pending_unattributable_conflict"`，回答标记“待核实”，不当作确定事实。
- 删除冲突、修改合成数据、降低 `EvidenceLedger.resolve()` 的 ID/hash/refetch/scope 校验均未发生。

### 2.2 Claim 运行时计数

`compose_investigation()` 现在返回第四个值 `claim_counts`，由实际执行路径填充：

- `claims_submitted`
- `claims_accepted`（最终保留）
- `claims_discarded_by_validation`
- `claims_discarded_by_conflict`
- `claims_retained_with_unresolved_conflict`

并写入 `InvestigationResult.claim_counts`；评测行与汇总新增 `claim_counts` / `ClaimObservability`。不是从最终 claims 反推。

### 2.3 终止原因

`finish_termination_reason()` 不再因“存在任意冲突”或“保留了一个 claim”直接定论：

- 存在 `affects_submitted_claim` 或 `unattributable` 冲突 → `conflict`；
- 所有冲突都被验证为 `independent_of_submitted_claims` 时，才按 gap、claim、工具失败情况判断；
- 后续 task coverage / missing targets 仍可把 `sufficient` 降为 `insufficient`。

### 2.4 事实覆盖评测

`_fact_score()` 只对可识别的回答正文计分：

- 新渲染格式：只提取【有引用支持的原文摘录】/【待核实的原文摘录】中的 claim 行，去掉引用尾注；
- 旧渲染格式或没有 claim 区段：`task_fact_coverage=null`，`task_fact_coverage_status="unverified_no_answer_body"`；
- 纯文本 answer（无区段头）仍按全文处理，兼容既有离线适配器测试；
- `aggregate()` 不把未识别正文的行计入 TaskFactCoverage 分母，并单独输出 `TaskFactCoverageUnverifiedRows`；
- 不再用【证据】【缺口】【冲突】里的关键词制造满分。

人工 `TaskSuccessRate` 仍完全独立，未改动标签、Gold Evidence 或历史报告。

## 3. 修改文件

- `src/investigation.py`：Conflict 归属字段、Claim 验证状态、`InvestigationResult.claim_counts`、结构化冲突保留 fields/subject。
- `src/agents/answer_agent.py`：按 claim 计算冲突关系；局部保留/撤回 claim；更新渲染区段。
- `src/agents/react_agent.py`：四元组 compose 结果、claim 计数、`finish_termination_reason()`、冲突合并去重。
- `benchmark/tasks/scoring.py`：回答正文提取、TaskFactCoverage 可评估性、未验证行统计、claim observability。
- `tests/test_investigation.py`：适配 compose 四元组返回值。
- `tests/test_conflict_scope.py`：新增 8 个回归测试。
- `benchmark/tasks/replay_conflict_scope.py`：cross-atlas 确定性离线复现脚本。

## 4. 测试结果

全量离线测试：

```powershell
python -m unittest discover -s tests
```

结果：`Ran 198 tests ... OK`（修复前 190；新增 8 个冲突作用域/评测回归测试）。

新增测试覆盖：

1. 无直接关联的 `ack_mode` 冲突不清除独立且通过校验的 claim。
2. 与 claim 引用证据直接重叠的冲突，不输出该 claim 的确定结论。
3. 多 claim 时仅撤回受影响 claim，其他 claim 保留。
4. 无法归属的冲突将保留 claim 标记为待核实，且 termination 不为 `sufficient`。
5. 篡改 Evidence ID/hash 的 claim 与 conflict 引用仍被严格拒绝。
6. 证据/缺口/冲突区段中的关键词不能单独获得满分 fact coverage。
7. subject + field 共同决定归属；subject 不匹配时判为无直接关联。
8. 既有 normal/conflict/abstention/decision-history 测试全部通过。

## 5. 离线复现

```powershell
python -m benchmark.tasks.replay_conflict_scope
```

该脚本读取历史 Trace 的冲突引用，重建 cross-atlas 固定输入。历史 Trace 只保存最终 `claims=[]`，没有保存模型提交的 claim，因此脚本中的两条 claim 是确定性 fixture，**不是真实模型响应的回放**。输出结果：

- 历史冲突数：2
- `claims_submitted=2`
- `claims_accepted=1`
- `claims_discarded_by_conflict=1`
- `claims_retained_with_unresolved_conflict=1`
- 受影响 claim 被撤回；另一条保留并标记 `pending_unattributable_conflict`
- `termination_reason="conflict"`，不是 `sufficient`

## 6. 仍无法自动判定的冲突

以下情况会保守标记为 `unattributable`，而不是假定无关：

- 模型只报告自由文本冲突，引用 Evidence 与 claim 的 ID/hash 无交集，且双方没有可用的 assertion 字段或 subject。
- Claim 引用的 Evidence 没有 `metadata.assertions`，claim 原文也没有出现冲突字段名。
- 中文语义相关但字段名/元数据不一致的冲突（当前不做语义 entailment）。
- subject 只从 Evidence metadata 读取；不从自由文本自动抽取实体。

对 cross-atlas 的 B/C 冲突：`0403`（issue）与 `e8a6`（C 提议）都没有 `metadata.assertions`。当模型提交的 claim 不直接引用这两条证据时，系统无法仅凭可验证元数据判断该冲突是否影响这条 claim，因此会保留 claim 但标记待核实；若 claim 直接引用其中一条，则按 Evidence ID/hash 判为直接影响并撤回。这是本次修复后仍保留的自动化边界。

## 7. 重新运行 cross-atlas（需人工授权，未执行）

使用与 `third-batch-online-cross-atlas-01` 相同的模型环境变量（不要回显 `SILICONFLOW_API_KEY`）：

```powershell
$env:SILICONFLOW_MODEL = "deepseek-flash"
$env:SILICONFLOW_BASE_URL = "https://api.deepseek.com"   # origin 必须与 --approved-origin 完全一致
python -m benchmark.tasks.run_online `
  --online `
  --approved-origin https://api.deepseek.com `
  --output benchmark/reports/third-batch-online-cross-atlas-02 `
  --task cross-atlas `
  --max-tasks 1 `
  --max-model-rounds 5 `
  --max-tool-calls 8 `
  --max-output-tokens 2048 `
  --deadline-seconds 120
```

`--output` 必须是尚不存在的新目录；命令会拒绝覆盖历史报告。该命令未在本阶段执行。
