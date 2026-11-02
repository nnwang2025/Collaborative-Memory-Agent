# 调查完整性与 live Evidence 上下文改进

日期：2026-10-29。沿用单 Agent ReAct、SQLite Evidence、Ledger、只读工具和已有协议校验。没有迁移数据库、重建索引、更改旧 fixture 算法、调用收费模型或提交/推送 Git。

## 1. 修改前的确认问题

历史报告 `benchmark/reports/deepseek-smoke-protocolfix-20261026-01.json` 中，multi_round 虽为 passed，但只取得三条 Commit：search_memory=3、search_docs=0、search_issues=0。原判据仅比较工具次数和 Evidence 数量，无法发现缺少评审文档。

文档导入保留整篇原文，但没有 event_time；严格 after 过滤正确排除日期未知的记录。问题是没有后续补查和相应任务覆盖判据，不应把日期未知改造成满足查询时间。

此前 ToolExecutor 保留原始 Evidence，但 Observation 固定发送正文前 700 字符。长文尾部即使命中 BM25，也不会展示给模型。Ledger 虽按 ID/hash 去重，消息历史仍不断累积相同正文。

## 2. 三层检查相互独立

| 层次 | 当前检查 | 不代表什么 |
| --- | --- | --- |
| 工具执行 | 实际尝试、错误、重试、恢复、预算和 Trace | 调用多次不代表任务完成 |
| 证据可信性 | ID/hash/scope、Ledger 观察、父原文回取、逐字摘录及片段定位 | 引用有效不代表覆盖所有问题或语义蕴含已验证 |
| 任务覆盖 | Runtime 显式来源缺口；smoke 独立的固定事实与关联检查 | 来源齐全不代表通用推理全部正确 |

`check_multi_round_evidence()` 在评测侧要求：Commit 决策原因、后续调整提交、评审文档都出现在已接受引用中；两个提交的事件时间有先后关系；后续实际工具过滤使用此前取得的决策时间；无日期文档的时间未知必须披露。它复用已有跨来源身份检查，没有改问题、语料或预期事实来迁就运行结果。

其中 chronology_verified 仅检查“决策提交→调整提交”，不证明评审文档处在该区间。文档时间仍为 null。task_success 继续待人工判断，不把规则检查标为通用语义成功。

Runtime 不导入 smoke、Benchmark 或预期 Evidence ID。它只检查用户问题中明确出现的多种来源词与实际 Ledger 来源，返回 task_coverage，并标记 semantic_coverage=unverified。显式来源未覆盖时追加缺口，不能 sufficient；预算耗尽时同样保留缺口。关键词检查范围刻意较窄，不能替代完整语义目标理解。

同时移除旧 Prompt/格式反馈中“至多两条摘录”的数量建议，改为按用户任务所需事实给出短摘录，防止它与多目标任务的覆盖要求冲突。原有 Claim Schema 上限、总预算和逐条引用校验不变，不把合成答案写入 Prompt。

## 3. 有界补查日期未知资料

仅对 live 路径，当带 after/before 的成功检索没有 Evidence 时，默认最多额外执行一次只读查询：

```text
原条件：保留 scope / source_types / evidence_ids / decision_id / known_at 等
补查：after=null，before=null，undated_only=true
```

undated_only 只允许 event_time=null 的记录；与 after/before 同时使用会被 Schema 拒绝。已知日期落在区间外的记录不会混入。不会放宽项目、数据集、证据身份或内部记忆的 known_at。

补查受 Budget.max_time_relaxations（默认 1）、总工具调用次数、工具超时及全任务 deadline 限制，不增加模型调用。原始空结果与补查分别记录 tool_attempt，后者带 recovery_kind=undated_time_relaxation；Observation 同时保留原/有效过滤和“并未证明处于原范围”的说明。

取得日期未知文档后，Runtime 强制保留时间缺口。若工具预算不足，直接披露无法补查，不偷偷继续执行。若候选非空但不充分，目前不会自动放宽；这是本轮的明确边界。

## 4. live 证据实际数据流

```text
ingest → 完整父 Evidence → SQLite 追加版本（原 ID/hash 不变）
  → scope/source/time 过滤 → 原有父记录 BM25 Top-K
  → ToolExecutor：保留完整 content，增加查询相关 excerpt
  → Ledger：父身份及 excerpt 校验
  → Observation：相关片段 + 父来源/时间/定位
  → 请求上下文：原问题 + 完整证据索引/工具状态 + 有预算的片段
  → AnswerAgent：仍按父 Evidence ID/hash 回取完整原文，验证引用
```

长文的相关片段由既有 BM25 对内存中的重叠字符窗口评分得到，默认最多 700 字符；短记录保留全文。没有数据库子块迁移、FAISS、Embedding、复杂 Reranker 或新的证据 ID。

Excerpt 绑定父 evidence_id、content_hash、project_id、dataset_kind；char_start 为零基闭区间起点，char_end 为开区间终点，单位为 Python Unicode codepoint；行号从 1 开始。验证要求 text 等于父原文切片，字符边界、行号和身份都吻合。

Ledger 在批量登记前校验所有片段，篡改正文、hash、scope、边界或行号都会拒绝，批量登记不留下半批结果。模型的 Action 不接受自造片段或片段 hash；最终引用仍使用父 Evidence 的原有 ID/hash。父完整 content、来源及原 locator 保留，不会被展示片段覆盖。

## 5. 多轮上下文与预算

live 请求重新构造为：固定系统规则、原始问题、当前调查状态、最近 Action/Observation。状态包含全部有效 Evidence 身份和来源索引、所有已知片段 locator、已执行工具/错误、Decision Memory、来源覆盖、未解决缺口和冲突。

相同 ID/hash/字符区间的片段只保留一份正文；重复 Observation 保留身份与 locator 并注明正文已在索引视图中。同一父 Evidence 的不同查询片段可同时保留，不错误合并为一个片段。修复格式错误时也不会丢失冲突或时间未知状态。

| 预算 | 单位与行为 |
| --- | --- |
| max_context_bytes，默认 48000 | 单次序列化 messages 的 UTF-8 字节政策预算，不冒充 provider 的精确 Token 或窗口元数据 |
| max_tokens，默认 64000；smoke 90000 | 全任务累计 Token 预算；provider usage 优先，缺失时保守估算并保留 estimated |
| max_output_tokens，默认 2048 | 受剩余累计额度限制的单次输出上限 |
| 轮次/工具/deadline | 原有约束保留，额外补查也计入工具预算 |

超出请求字节预算时，确定性地省略完整的可选片段正文，保留 ID/hash、来源、locator、时间状态和 body_omitted 标记；不从中间切断引用。若原问题、证据索引、工具/错误等必要状态本身已超预算，则在发起模型请求前以 context_byte_budget 终止，并披露未完成状态。

这不是付费摘要，也没有声称精确获知模型窗口大小。长问题/大量定位信息仍可能让必要状态超限，安全停止优于隐式丢失引用链。旧 fixture 路径不启用这些 live 上下文改造。

## 6. multi_round 改进前后

正式离线探针：[context-multi-round-offline-20261029-final-v2.json](../benchmark/reports/context-multi-round-offline-20261029-final-v2.json)。该文件明确标为 scripted_model_real_git_tools_offline，online_executed=false；有最终代码 hash，不是新的真实 DeepSeek 结果。

| 项目 | 历史真实运行 | 本轮离线探针 |
| --- | --- | --- |
| 模型动作数 | 4 | 4 个脚本动作 |
| 工具实际尝试 | 3 | 4：新增一次日期未知补查 |
| Commit | 3 条 | 3 条 |
| 文档 | 0 条 | 1 条，event_time=null |
| 三个预期角色 | 漏评审文档 | 原因、后续提交、文档均取得且引用 |
| 结束状态 | insufficient | insufficient，强制披露文档时间未知 |
| 原 smoke 判据 | passed（误判） | 新覆盖检查通过；语义仍待人工复核 |

探针实际轨迹：search_memory(3) → search_docs(after，0) → search_docs(undated_only，1) → search_issues(0) → finish。第二次空结果不会再触发补查，因为全任务补查上限为 1。

负例测试同时证明：即使有多个工具和多个 Commit，只要遗漏评审文档，新 multi_round 判据就不能通过；Runtime 也会披露用户所提来源未覆盖。

## 7. 全量测试与 Benchmark

实际执行：

```powershell
python -m unittest discover -s tests -q
python -m benchmark.runner --split test --output benchmark/reports/context-before-20261029
python -m benchmark.runner --split test --output benchmark/reports/context-after-20261029-final-v2
python -m compileall -q src tests benchmark
git diff --check
```

修改前：140 tests，140 通过、0 失败，17.680 秒。最终：**152 tests，152 通过、0 失败，19.305 秒**。新增 12 项包含真实临时 Git、严格日期/未知日期、预算限制、尾部片段、定位篡改原子拒绝、重复正文、请求裁剪、必要状态溢出、协议修复时冲突保留、来源缺口和完整 multi_round 正负例。原有同工具多来源、跨项目/fixture 隔离、协议与 Decision Memory 测试全部保留通过。

两个 Benchmark 使用未修改的 cma-v1-fb8e82059066d95c 快照和标签；每次各 28 次后端调用，均无模型请求。before 与最终 after 的全部非延迟指标相同：

| 指标 | Workflow 前→后 | ReAct offline_fallback 前→后 |
| --- | --- | --- |
| Hit@5 | 85.71% → 85.71% | 78.57% → 78.57% |
| Recall@5 | 71.43% → 71.43% | 60.71% → 60.71% |
| MRR | 0.8000 → 0.8000 | 0.6548 → 0.6548 |
| Citation Validity | 78/78 → 78/78 | 74/74 → 74/74 |
| Evidence Coverage | 71.43% → 71.43% | 71.43% → 71.43% |
| 模型调用 / 工具尝试 | 0/20 → 0/20 | 0/22 → 0/22 |
| unsupported / ungraded | 5/9 → 5/9 | 5/9 → 5/9 |

Task Success、恰当拒答等无人工分母的指标仍为 null，不制造提升。Decision 专项仍为当前版本 2/3、历史版本 0/2、替代关系 1/1、待审未正式化 1/1、现实状态 unknown 3/3。既有 Workflow 故障注入仍有 2 次运行异常，本轮没有将其伪装修复。

实测平均/P95（ms）：Workflow 175.25/2382.87 → 38.55/466.77；ReAct 5.93/10.93 → 5.94/9.88。冷初始化、进程调度和并行回归负载不同，这不是受控性能实验，不能宣称加速。无 Key fallback 也不会验证真实模型如何使用新上下文；相关改进由另行标明的脚本模型 Runtime 集成测试覆盖。

最终结果在 context-after-20261029-final-v2；context-after-20261029、context-after-20261029-final 及不带 final-v2 的探针是中间运行，全部保留而未覆盖。v2 包含最终按任务事实覆盖组织 finish 的指导。最终 Benchmark 和探针代码 hash 已核对。

## 8. 文件与边界

| 文件 | 原因 |
| --- | --- |
| src/investigation_context.py | 新增可校验片段、显式来源缺口和确定性请求上下文构造 |
| src/agents/react_agent.py | 接入 live 视图、增量 Observation、字节预算及有界日期未知补查 |
| src/investigation.py | 新过滤/预算字段、片段登记校验、可解释 Trace/结果字段 |
| src/retrieval.py | 仅增加 undated_only 过滤，不改排名算法 |
| src/llm_smoke.py | 固定事实角色、提交时间关联与 multi_round 覆盖判据；复用跨来源校验 |
| benchmark/runner.py | 记录新增配置和上下文代码 hash，不改 corpus/labels/评分指标 |
| tests/test_investigation_context.py | 新增 12 项离线回归及可复用探针 |
| docs/INVESTIGATION_CONTEXT_IMPLEMENTATION.md、README.md | 实现与验证记录 |
| benchmark/reports/context-* | 新建的前后比较和明确离线探针 |

尚未解决：显式来源词检查不是语义目标解析；文档日期仍未知；自动补查仅覆盖空结果且有一次上限；父文档未进入 Top-K 时片段选择不能救回；局部窗口可能切断语义，需要模型更换查询；元数据过大时会安全终止；真实 LLM 的任务完整性、输入用量与稳定性未重新验证。没有修改历史报告、源数据、冻结标签或用户模型配置。

## 9. 供操作人手动执行的真实冒烟

本轮没有创建以下在线文件。沿用已配置的 DeepSeek 进程环境，在明确授权费用及合成数据传输后由操作人执行：

```powershell
python -m src.llm_smoke --online --approved-origin https://api.deepseek.com --max-output-tokens 2048 --max-protocol-retries 2 --output benchmark/reports/deepseek-smoke-context-20261029-01.json
```

已有路径会在请求前被拒绝；如之后已占用请换新后缀。检查 multi_round_check、task_coverage、context_usage、undated_time_relaxation 和 Claim 来源，不能仅看 status 或 sufficient。真实评审文档时序缺乏资料时，正确披露未知仍可能合理地结束为 insufficient。
