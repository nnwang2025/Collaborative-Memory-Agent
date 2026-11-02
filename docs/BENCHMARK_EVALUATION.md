# 第五轮：独立 Benchmark 与离线后端对比

执行日期：2026-10-22，Windows / Python 本地环境。正式结果为 [offline-final/summary.json](../benchmark/reports/offline-final/summary.json)，逐题原始结果为 [results.jsonl](../benchmark/reports/offline-final/results.jsonl)。

已完成独立合成语料、标签验证、真实后端适配、指标计算、人工评分入口和回归测试。真实 LLM **未执行**，`online_status=pending_authorization`。本报告不证明 ReAct 优于 Workflow；离线 fallback 不是模型自主调查。

## 1. 核对现有实现

核对了前四轮审计/实现文档，以及 API、CoordinatorAgent、LangGraph、ReActAgent、AnswerAgent、EvidenceStore、DecisionStore、检索、Skills 和测试代码。

- 默认 CoordinatorAgent 使用 LangGraph 固定路由/fan-out/fan-in；本次调用真实 `ask()`，没有替换为检索桩。
- ReActAgent 有 Action 校验、工具重试、Ledger、引用回取、预算、Trace；无 Key 时走 `offline_fallback`，不调用 LLM，也不会根据 Observation 自主规划下一步。
- 两个后端都经 ScopedSkill 查询同一 Evidence/Decision 存储。固定 Workflow **已有当前 Decision Memory 查询**，不能称它完全不支持 Decision Memory。
- DecisionStore 的 `known_at` 和历史版本查询已实现；现有离线自然语言路由不会从问题自动构造这些过滤条件。
- 原 95 项测试保留。本轮没有重写 Agent、图或检索算法，没有增加 Agent 角色。

## 2. 数据来源、冻结与标注

本轮独立作者脚本为 `benchmark/scripts/build.py`，不使用旧 mock 数据生成器、`src/evaluate.py` 的手写题库或 Agent Prompt 生成答案。运行器不导入作者脚本。

| 项目 | 实际规模 |
| --- | --- |
| Evidence | 27 条：Git 6、Issue 6、文档 11、Chat 2、Log 2 |
| 项目分区 | bench-atlas 12、bench-zephyr 13、bench-orbit-dev 2 |
| 决策 | 3 个主题、4 个正式版本、5 个候选，含 4 次接受和 1 个待审候选 |
| 测试题 | 14 道，七类各 2 道 |
| 开发题 | 2 道，使用独立项目 bench-orbit-dev，不进入测试指标 |
| 人工标签复核 | 尚未完成；16 道均为 pending_human_review，reviewers 为空 |

七类分别是 single_source、cross_source、multi_round、decision_history、conflict、insufficient、tool_failure。Git 记录由两个临时真实仓库各 3 次提交产生，保留 SHA 和事件时间；仓库 URI 使用明确的 `git+benchmark://` 合成标识，不冒充远程真实项目。

全部记录 `metadata.synthetic=true`。`dataset_kind=live` 用于验证现有 live 数据通路，**不表示这些是生产业务数据**。默认 fixture 数据未参与实验。

固定种子为 `20261022`。快照：

```text
corpus_snapshot_id = cma-v1-fb8e82059066d95c
manifest SHA-256   = 84f5f8dc2d41331964484f8518e91d87142688ca5acc6d80b12d6a0c7380655a
```

`benchmark/manifest.json` 冻结 corpus、cases、labels 五个文件的字节数及 SHA-256。快照 ID 由 corpus 文件内容计算，避免自引用；Manifest 还覆盖公开问题和隐藏标签。加载时拒绝缺失/新增文件、内容修改、目录逃逸和快照 ID 不一致。运行前将静态 corpus 恢复到新临时 SQLite，拒绝复用已存在的 DB；作者脚本拒绝覆盖已有快照文件。测试验证本机两次重建完全一致；跨操作系统重新生成 Git 对象的字节级一致性尚未验证，使用随附冻结语料不需要重建 Git 仓库。

每题公开信息保存在 `cases/public.json`，标准证据 ID/hash、预期行为、预期终止、评分说明、决策版本/冲突/缺失资料约束保存在 `labels/gold.json`。通过 case_id 关联即可得到完整题目定义。标准答案在作者阶段写成静态文件，不在 Agent 执行时生成。

标签验证器实际检查证据存在、项目/数据集归属、内容 hash、版本存在、known_at 下当前版本和审核记录、supersedes 关系、待审无正式版本、两个明确相反的结构化断言，以及不足题的关键事实字段在封闭语料中缺失。这是结构和场景一致性验证，不能代替独立语义标注，也不能证明现实世界缺少某事实。

## 3. 数据流与标签隔离

```text
作者脚本 → 冻结 corpus / public cases / hidden labels / manifest
                                  ↓ manifest、标签验证（评测侧）
每题每后端新建临时 Evidence DB + Decision DB
    ↓ public question + project_id + dataset_kind
真实 CoordinatorAgent.ask 或 ReActAgent.run_result
    ↓ ScopedSkill → 原有检索 / DecisionStore → 原有回答与引用
只读观测原始工具事件、答案、原生 Trace
    ↓ 执行结束
评测侧关联隐藏标签 → 指标 / unsupported / ungraded → 新报告目录
    ↓ 可选的显式人工评审文件
grade 脚本生成另一份报告；不重新执行 Agent、不覆盖原报告
```

`run_backend()` 只接收严格 PublicCase、临时 DB 路径、故障注入配置，不接收 gold、预期回答或评分说明。故障注入仅控制指定 Skill 超时，不把其标准答案传给 Agent。运行目录仅有两个 corpus DB；模型可用工具没有任意文件读取能力。回归测试拒绝公开输入夹带 gold，并在真实后端运行期间阻止 Path.read_text 读取 labels。

这是当前工具能力和数据流的隔离，不是操作系统文件沙箱；开发者进程仍有工作区读权限。将来增加任意文件/代码执行工具时必须重新审核隔离边界。

## 4. 实验配置与公平性

| 配置 | Workflow | ReAct |
| --- | --- | --- |
| 入口 | CoordinatorAgent.ask → 真实 LangGraph | ReActAgent.run_result |
| 本次模式 | use_llm_routing=False，关键词路由 | 无 Key 的 offline_fallback |
| 模型 / 温度 | null / null | null / null |
| 语料 / scope | 相同冻结语料，各自新 DB、相同题目 scope | 同左 |
| 检索 | 现有 scoped BM25，无 FAISS/模型下载 | 同左 |
| 原生候选限制 | Skill 默认 top_k=5；Memory 可附加版本来源，回答重排最多 8 | 相同 Skill；ToolExecutor 最多交付 5 条/次 |
| 预算 | 现有 Workflow 没有统一模型预算 | 5 决策轮、8 工具调用、64000 Token、120 秒、1 次重试 |
| 输出观测 | Skill 事件、原文答案、实际显示的 ID/hash、回答重排 | 上述信息及原生 InvestigationResult/Trace |

没有为赢得对比削弱 Workflow，也没有用 Gold 驱动 ReAct 工具选择。结果使用不同 run_id，串行执行，数据库各自复制，不读取另一后端结果。进程 Key 临时置空，urllib 网络入口阻断。包版本 langgraph=1.2.2、pydantic=2.13.1；正式 summary 保存应用、评测代码和 Prompt 的 SHA-256。

两个协议的排名含义不同：Workflow 使用真实回答阶段 `_top_relevant` 顺序，再补充按工具名固定排序的观察候选；ReAct 使用原生 Trace 中真正交付给 Agent 的 Evidence 顺序，去重后取前 K，不能使用 ToolExecutor 截断前候选抬高召回。故 @K 是**原生后端证据可达性诊断**，不是相同候选排序器的纯检索消融。Citation Coverage 使用最终实际引用，与该排名独立。

两个离线模式的多轮反馈、故障后自主换源、问题日期转 known_at 均未执行，因此每后端 5 题标记 unsupported（多轮 2、恢复 2、历史时点 1）；剩余 9 题 ungraded。真实 ReAct 架构支持相应 Action/过滤接口，不等于本次在线效果已验证。`decision-zephyr` 的历史任务仍保留 ungraded，并通过实际返回版本检查揭示其历史缺失，不因一次缺失就把架构能力伪装为不存在。

## 5. 指标定义与分母

先对检索结果按实际观察顺序去重。令 G 为题目的 Gold ID 集合，R5 为前 5 个 ID，C 为 ID/hash/scope 回取成功的最终引用 ID 集合。

| 指标 | 定义与适用分母 |
| --- | --- |
| Hit@5 | 每题 1[R5∩G 非空]，对有 Gold 的题目宏平均 |
| Recall@5 | 每题 \|R5∩G\|/\|G\|，对有 Gold 的题目宏平均 |
| MRR | 全部观察排名中首个 Gold 的排名倒数，无命中为 0；不截断于 K |
| Task Success | 人工显式判成功数 / 已人工评分且非 unsupported 题数 |
| Citation Validity | ID/hash/project/dataset 回取有效的实际引用次数 / 实际引用次数；不是语义蕴含准确率 |
| Evidence Coverage | 每题 \|C∩G\|/\|G\|，对有 Gold 的题目宏平均；不限制 K |
| Appropriate Abstention | 人工确认恰当拒答数 / 已人工评审且非 unsupported 的不足题数 |
| Tool Failure Recovery | 故障后实际通过后续工具获得 Gold 的题数 / 确实触发故障且后端支持恢复的题数；仅操作恢复，不等于语义任务成功 |
| Invalid Action | Action 协议错误次数 / 实际模型调用次数 |
| Duplicate Tool Call | Runtime 判定重复的 Action 请求次数 / 模型工具 Action 请求次数；重试不是重复规划 |
| 延迟 | 每次后端调用的本地实测时长；均值、P95 使用 nearest-rank |
| 调用与 Token | 工具实际尝试次数、模型实际调用次数；provider input/output 与 estimated 分开累计 |
| 预算耗尽 | 原生结束原因为 budget 的题数 / 全部执行题数；Workflow 无对应全局预算，此处 0 不能证明其预算管理正确 |

unsupported 不进入成功、拒答、恢复率分母，但其真实检索、引用和成本仍保留在全量诊断中；不会把这些运行当作正确拒答。没有合法分母一律 null，不能解释为 0% 或 100%。无人评审时，不用 sufficient、答案包含关键词或命中 Gold 推算 Task Success。

决策专项检查针对返回的结构化 Decision Memory：当前指针与指定 known_at 标签相等、Gold 历史版本是否全部返回、返回版本的 supersedes 是否匹配、确实查询过记忆并取得相关证据时待审项是否没有正式版本、非空返回版本的 world_state 是否均为 unknown。最后一项是状态字段检查，不是对回答全文语义的自动担保。

## 6. 实际结果

正式对比执行 14×2=28 次后端调用，均离线。正式文件为 offline-final；offline-v1、offline-v2 是评测开发期间的中间产物，不作为正式结果，具体差异见 reports/README.md。

| 指标 | Workflow | ReAct offline_fallback |
| --- | --- | --- |
| Hit@5（14 题） | 12/14 = 85.71% | 11/14 = 78.57% |
| Recall@5（宏平均，14 题） | 71.43% | 60.71% |
| MRR（14 题） | 0.8000 | 0.6548 |
| Citation Validity | 78/78 = 100% | 74/74 = 100% |
| Evidence Coverage（14 题） | 71.43% | 71.43% |
| Task Success / Appropriate Abstention | null，0/0 | null，0/0 |
| Tool Failure Recovery | null，0/0（2 题不支持） | null，0/0（2 题不支持） |
| Invalid Action / Duplicate Call | null，0/0 | null，0/0 |
| 平均 / P95 延迟 | 38.25 / 465.43 ms | 5.40 / 9.31 ms |
| 模型调用 / 工具尝试 | 0 / 20 | 0 / 22 |
| provider input / output / estimated Token | 0 / 0 / 0（未调用模型） | 0 / 0 / 0（未调用模型） |
| 预算结束原始计数 | 0/14（全局预算不可测） | 0/14 |
| unsupported / ungraded | 5 / 9 | 5 / 9 |
| 运行异常 | 2 次 TimeoutError | 0 次未捕获异常；2 次 tool_failure |

所有最终引用身份可回取，不代表所有必要证据齐全或结论正确。两个后端最终证据覆盖相同；@K 排名差异受原生输出顺序影响。延迟包含 Workflow 首次 LangGraph 初始化、无预热，而且两进程实验与回归测试同时执行；它不是线上模型延迟，也不是严格性能实验，不用于宣称速度优势。样本量不足，不做统计显著性推断。

| 决策结构检查 | 两个后端本次结果 |
| --- | --- |
| 当前确认版本（含已知历史时点探测） | 2/3 |
| 历史版本齐全 | 0/2 |
| supersedes 链接 | 1/1 |
| 待审候选未变正式决定 | 1/1 |
| 返回版本现实状态保留 unknown | 3/3 |

这组全量结构诊断包含 unsupported 的 historical 探测。它帮助定位接口缺口，不纳入人工任务成功率。返回 C 的 supersedes=B 正确，不等于已经获取 B 的完整历史版本。

## 7. 三个代表性失败

1. **cross-atlas：跨来源漏证据。** 两个后端都只调用 search_memory 一次，命中讨论 `ev_9ccc9fcf6f0f451cfc3a4ecc8d405ebe7d8f54e05f1bd49b7d03740f5124b7aa`，没有获取实现提交 `ev_6c9d44174e8d75343a4c992eb5be7d5be7f3f3ed05ae58a1a56ab5341c47d0b0`，Gold 覆盖 1/2。关键词命中不等于完成关联；两行均 ungraded。
2. **decision-atlas：问题时点未转过滤。** 题目要求截至 2025-02-20；标签与实际审核历史一致，此时 B 正式确认、C 待审。两后端默认查询当前记忆，返回 C `dv_d46c1881b3b9a911a39e1f1a4efc1927`，未返回当时 B，当前/历史检查均失败。该题在这两个离线模式为 unsupported；DecisionStore 的显式 known_at 测试通过，不应误称数据库历史查询本身坏了。
3. **recovery-atlas：超时后不换源。** search_issues 持续注入 TimeoutError；Workflow 整图异常，ReAct 重试后 tool_failure，均未访问含应急方法的文档。两题对应的工具恢复能力未在线验证，状态 unsupported；不是“成功拒答”。同样现象发生在 recovery-zephyr 的文档故障。

附加观察：decision-zephyr 虽拿到当前 C 和其 supersedes 指针，却没有返回 B 的历史版本；multi-atlas 只拿到 2/3 Gold，没有沿线索再调查。

## 8. 原始轨迹示例

来自正式结果的 recovery-atlas / ReAct，run_id=`647837d0-ff63-438a-bc10-7d80766c8a84`。以下仅摘录原生 Trace 的字段，未构造模型行为：

```text
1 tool_call    search_issues
2 tool_attempt search_issues → temporary_tool_error, retryable=true, evidence_ids=[]
3 tool_attempt search_issues → temporary_tool_error, retryable=true, evidence_ids=[]
4 finish       termination_reason=tool_failure
model_calls=0, tool_attempts=2, status=unsupported
```

Workflow 只有适配层观测的 Skill 事件和原始答案，没有被伪造为原生 ReAct Trace。并行工具事件的 order 是实际完成顺序；不把它当作模型按反馈选择工具的证据。

## 9. 真实 LLM 冒烟状态

复用已有 src/llm_smoke.py 五场景。协议是当前 ReAct 使用的 OpenAI 兼容 `/chat/completions`，JSON tool_call / finish，filters 和 ID/hash 引用由 Runtime 校验；provider temperature=0.1，默认模型 Qwen/Qwen2.5-14B-Instruct，可由进程配置覆盖。

只检查了 Key 环境项是否存在：SILICONFLOW_API_KEY=False、OPENAI_API_KEY=False；`.env` 存在但未读取、打印或修改，不确认其中配置是否有效。没有联网或尝试收费服务。

实际运行 `python -m src.llm_smoke`：临时 Git 3 次提交 + 1 份 Markdown，导入 4 条合成 Evidence，列出 single_source / cross_source / multi_round / insufficient / tool_recovery，返回 online_status=pending_authorization、online_executed=false。它只准备数据，不构造模型客户端。

获得对服务、费用和合成数据传输的明确授权后，操作人可运行以下命令；**本次未运行**：

```powershell
python -m src.llm_smoke --online --approved-origin https://api.siliconflow.cn --output benchmark/reports/online-smoke-new.json
```

origin 必须与进程中的实际 HTTPS provider 配置完全匹配，报告路径必须新建；不会跟随重定向，不自动加载 .env。真实模型名称、origin、UTC 执行时间、配置、延迟、Action 错误、无效工具、重复请求、实际顺序、结束原因、用量及 estimated 标记会随合成数据轨迹保存。语义任务成功和 task_success_rate 保持 null，待人工复核；online_protocol_passed 只代表协议/轨迹检查。

本轮补齐了这些遥测字段及 pending_authorization 明确状态。没有在线成功率、在线 Token 成本或模型质量结论。完整 Benchmark runner 当前也只实现离线适配；在线全量双后端对比尚待授权及后续相同 provider/budget 适配，不能把冒烟脚本当成已经执行的线上 Benchmark。

## 10. 复现命令与真实测试

在项目根目录执行：

```powershell
python -m src.llm_smoke
python -m benchmark.dataset
python -m benchmark.runner --split test --output benchmark/reports/my-new-run
python -m unittest discover -s tests -v
```

本轮正式运行 `python -m benchmark.runner --split test --output benchmark/reports/offline-final`，exit 0，生成 28 行原始结果。标签验证 exit 0：27 证据、14 测试、2 开发、每类测试 2 道；仅结构有效、待人工复核。

全量回归结果：**115 tests，115 通过，0 失败，13.142 秒，OK**。原 95 项完整保留，新增 20 项。最终命令为 `python -m unittest discover -s tests -q`（同一测试集的 `-v` 运行也全部通过）。覆盖冻结重建、Manifest 篡改、Gold ID/hash/scope、冲突与不足约束、决策版本、标签隔离、指标口径、空 Gold 分母、真实后端适配、故障注入、unsupported/ungraded、显式人工评分、报告不可覆盖和开发集重复执行的非延迟指标一致。测试中的人工评分数据仅是单元测试输入，不是本报告的真实人工标注。既有脚本模型 Runtime 测试仍是离线协议测试。

`python -m compileall -q benchmark src tests` 和 `git diff --check` 均 exit 0；Git 仅提示工作区 LF/CRLF 转换，没有空白错误。正式报告中的全部 source_sha256 已与最终相关代码核对一致。

测试/Benchmark 需要临时 Git 和 SQLite 文件。本机默认沙箱对部分 Temp 路径受限，使用获批本地执行权限完成；没有网络权限请求。正式结果只写入本轮新建 benchmark 目录，业务 data、索引、用户资料未覆盖或迁移；未提交或推送 Git。

## 11. 标注复核与扩展到 70 题

先独立复核当前 14 道测试题及 2 道开发题的 Gold、必要来源、预期行为、时间范围和不足/冲突约束，再由另一名评审复核有争议项。实际未发生人工复核，不能称双人标注完成。

现有原始结果可交给人工按 expected_behavior、grading_notes、完整证据和答案逐条评分。将 case_id、backend、run_id、reviewer、task_success 布尔值，以及不足题的 appropriate_abstention 布尔值写到单独评审 JSON，使用 `python -m benchmark.scripts.grade --report benchmark/reports/offline-final --grades <人工文件> --output <新报告目录>`。此脚本绑定原 run_id 和 Manifest，拒绝错配、重复、伪布尔和 unsupported 评分，不调用模型、不覆盖原结果；记录 reviewer 和评审文件 hash。它不改冻结标签的待复核状态。

扩展需要新增 **56 道独立事件题（每类增加 8 道）**：

| 类型 | 新增事件设计 |
| --- | --- |
| 单来源 | 8 个不同故障或方案理由，分布在 Git、Issue、Docs、Log |
| 跨来源 | 8 组有实际 ID/时间引用的讨论—实现—审核关系 |
| 多轮 | 8 条必须先发现间接标识再定位后续来源的事件链 |
| 决策历史 | 8 组独立审核时间线，含拒绝、撤回、多个候选与 known_at 截止 |
| 冲突 | 8 对明确同环境/同时间矛盾事实，不能用不同环境伪造冲突 |
| 不足 | 8 个有相关历史但缺生产状态/批准人/测量值等关键记录的任务 |
| 恢复 | 8 个存在可替代证据的超时、空结果、非重试错误等故障场景 |

每组先建设可审计语料与独立项目，再写问题和独立复核标签；设置项目级 holdout。不能换词凑题、根据本次失败反向把 Gold 改成命中结果，或用测试集调 Prompt。扩展后发布新快照和 Manifest，当前 v1 不改写。70 题目标本轮未完成。

## 12. 下一轮优先事项

1. 在明确授权后完成五场真实 LLM 冒烟，人工核对语义；再为统一 runner 增加显式在线双后端适配、同模型预算和独立质量评审。当前不能比较模型自主调查优劣。
2. 将问题中的历史时点、历史视图与 decision_id 传入现有 Filters/known_at，确保查询过去审核状态；保持现实有效性 unknown，不能从当前确认指针推断生产现状。
3. 改善工具失败后的调查恢复，并为 Workflow 增加可观察的部分失败结果；使用现有 ReAct 的 Observation/换源接口验证失败恢复，不引入新 Agent。同时补齐跨来源缺证据时的继续调查。

尚未验证的部分还包括真实企业数据、跨服务故障、线上并发与稳定性、模型语义引用蕴含，以及 70 题的统计效果。本轮不把这些写成已实现或已通过。

## 13. 本轮文件变更

| 文件 | 本轮职责 |
| --- | --- |
| benchmark/__init__.py、scripts/__init__.py | 独立包入口 |
| benchmark/scripts/build.py | 独立事件语料、真实本地 Git、审核时间线和静态标签作者工具 |
| benchmark/corpus/evidence.json、decisions.sql、relations.json | 冻结证据、审核历史与冲突关系 |
| benchmark/cases/public.json、labels/gold.json、manifest.json | 开发/测试题、隐藏标签、冻结校验 |
| benchmark/dataset.py | 快照加载、标签验证、隔离恢复 |
| benchmark/runner.py | 两后端适配、只读观测、故障注入、状态与报告 |
| benchmark/metrics.py | 明确适用范围和分母的指标 |
| benchmark/scripts/grade.py | 运行后显式人工评分，不重跑、不覆盖 |
| benchmark/README.md、reports/README.md、reports/*/*.json* | 操作说明与实际结果 |
| src/llm_smoke.py | 复用五场景，补充授权状态、执行遥测、协议检查与语义未评分标记 |
| src/evaluate.py | 仅修正文档说明，明确旧启发式诊断不代表语义质量 |
| tests/test_benchmark.py | 新增 20 项离线回归 |
| README.md、docs/BENCHMARK_EVALUATION.md | 入口说明和本报告 |

Git 状态中还有此前四轮遗留的未提交文件；它们没有被恢复、提交或覆盖。本表仅列本轮主动新增/修改内容。
