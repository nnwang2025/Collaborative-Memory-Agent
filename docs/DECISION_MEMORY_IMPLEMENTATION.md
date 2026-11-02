# Decision Memory 演化、审核与真实模型冒烟准备

日期：2026-10-22。第四轮增量实现；没有Git提交/推送，没有更改用户模型配置、迁移真实数据或重建索引。原有未提交修改保留。

## 1. 第一阶段实际执行状态

先核对现有ReActAgent：支持SILICONFLOW_*和OPENAI_*进程环境配置，默认Qwen/Qwen2.5-14B-Instruct，OpenAI兼容/chat/completions协议；每轮provider timeout与第三轮Budget一致。已有69项离线测试，不等于真实模型验证。

仅检查进程环境项是否存在：SILICONFLOW_API_KEY=False、OPENAI_API_KEY=False；本地.env文件存在但没有查看内容、复制、输出或修改。因此没有确认其凭据可用性，也没有探测远程网络或计费服务。独立冒烟命令只读进程配置，不自动加载.env。

本次请求明确要求未获本次真实模型授权时报告待授权并继续开发，因此真实LLM在线测试**未执行，待授权**。没有因环境或配置文件可能含Key而自动请求模型。先执行python -m src.llm_smoke：生成临时Git仓库3次提交、1个Markdown记录，导入4条Evidence后清理，返回pending_authorization、online_executed=false。随后执行原69项回归，全部通过，再开始Memory开发。

### 显式冒烟命令

准备数据，不构造模型客户端、不读Key、不联网：

```powershell
python -m src.llm_smoke
```

只有操作人明确批准配置中的服务、费用和合成数据传输后，才运行以下形式的命令（本次没有运行）：

```powershell
python -m src.llm_smoke --online --approved-origin https://api.siliconflow.cn --output smoke-report-new.json
```

--approved-origin必须与当前配置的HTTPS origin完全一致；未知或不匹配origin在构造客户端前拒绝。不跟随重定向；配置不变，不自动下载模型，只发送本脚本生成的非敏感合成资料，绝不扫描用户仓库。已有报告不覆盖，输出父目录不存在也在请求前拒绝。无凭据返回not_executed_no_credentials，不会用离线fallback冒充在线通过。

5个场景：单来源历史查询、跨来源调查、根据上轮结果多轮调用、证据不足、注入Issue工具暂时失败后换源恢复。在线记录provider实际Action文本（Key防御性脱敏）、工具顺序、Evidence ID/hash、结束原因、Token及estimated、协议错误/重复调用/无效引用。独立报告为显式opt-in的合成数据记录，不改变第三轮默认Trace不持久化正文的策略。

状态严格区分：

| 层次 | 本轮状态 |
| --- | --- |
| 离线协议/Runtime | 已测试通过，包含原有69项 |
| 数据库/审核集成 | 已测试通过，使用临时SQLite |
| 冒烟数据与授权闸门 | 已准备、已离线测试 |
| 真实LLM在线通过 | 未声称 |
| 真实LLM在线失败 | 未发生在线执行，不能称失败 |
| 真实LLM未执行 | 待本次明确授权 |

冒烟passed仅为定义好的协议/工具轨迹检查，不是语义Benchmark，不证明完整答案正确性。每场默认7个决策轮次、90000累计Token上限，provider用量优先；不能把上界估算当账单。

## 2. 实际持久化模型

新增src/memory/decisions.py，复用EvidenceStore按ID/hash只读回取证据，另建独立SQLite：默认与Evidence数据库同目录的decisions.sqlite3，或可信配置DECISION_DB_PATH。禁止Decision数据库与Evidence数据库同一路径。读取不存在的库返回空，不建库。只有本地显式create/propose/review写操作才创建本轮表。

旧data/decision_memory.json继续作为legacy演示数据，未导入为已确认决策。新旧存储并存，无真实迁移，无Evidence表变更。

| 表 | 实际字段与职责 |
| --- | --- |
| decisions | decision_id、project_id、dataset_kind、title、created_at、current_confirmed_version_id、revision；稳定主题与唯一当前指针 |
| review_proposals | proposal_id、decision_id、proposed_statement、proposed_event_time、evidence_refs、source_run_id、review_status、created_at、reviewed_at、reviewer_id、review_note、risk_flags、base_revision、dedupe_key、investigation_context |
| proposal_observations | 每次不同run/context的追加风险观察；重复候选保持同proposal_id，新调查风险不会丢失，也不覆盖旧context |
| decision_versions | version_id、decision_id、revision、statement、status、event_time、recorded_at、supersedes_version_id、reviewer_id、reviewed_at、proposal_id |
| decision_evidence | version_id、evidence_id、content_hash、relation_type(supports/contradicts)；引用原始证据版本 |
| conflict_links | conflict_id、decision_id、两侧version/proposal ID、差异描述、resolution_status、创建/审核时间与审核备注 |
| decision_review_history | 追加记录accept/reject、审核者标签、前后revision、前后当前指针、审核时间与备注 |

版本status约束保留proposed/confirmed/superseded/retracted/unknown枚举。本轮将待审状态规范化放在proposal.pending；只有接受时才创建confirmed版本，替代时旧版转superseded。retracted/unknown为预留状态，没有声称已实现撤回审核全流程。

版本正文、业务时间、原始审核信息、版本替代关系不可覆盖：SQLite触发器阻止这些列更新和版本删除；Evidence链接不可改/删，review history不可改/删。允许事务内改变旧版本状态，正文与来源不变。触发器不是针对有权限修改数据库schema的管理员的安全隔离。

## 3. 从调查结果提出候选

DecisionStore.propose(decision_id, InvestigationResult, statement, refs可选, event_time可选)是明确输入的本地提议接口；没有额外LLM抽取模型，也没有在run_result结束时自动写Memory。

提议校验：

1. result使用第三轮InvestigationResult校验，固定scope。
2. 至少1条supports引用；所有refs必须在result.evidence_refs中，且可在同project/dataset的EvidenceStore按ID/hash回取。
3. statement是明确提交的候选陈述，不因为有引用就认定其语义成立；永久保留identity_only_not_semantically_verified风险。
4. gaps、conflicts、非sufficient结束、legacy来源和显式contradicts引用均有风险标记及原始context。
5. 去重键由decision_id、规范化statement、业务时间、排序后的ID/hash/relation构成，不依赖Python hash或run_id。完全相同内容/来源返回同proposal_id；不同证据或不同陈述保留独立候选。
6. 相同候选来自新run时，向proposal_observations追加context与风险，不覆盖初始调查。历史known_at查询过滤晚到观察，避免未来风险污染旧快照。
7. 有不同正文的待审候选或与当前正文不同的候选，生成待人工判断的差异链接，不自动把它标成逻辑矛盾，更不自动判旧决策失效。

即使result.termination_reason=sufficient也只能得到pending候选。候选校验/写入失败不改变当前确认版本。上传或传入InvestigationResult不等于证明某个真实run的身份，本地调用方必须可信；没有HTTP候选写入口。

## 4. 可信本地审核与版本演化

审核仅在DecisionStore.review和python -m src.memory.decision_cli中暴露；没有HTTP写路由，不在Tool白名单中注册propose/accept/reject。reviewer_id/--reviewer-label只是可信本地操作者提供的审计标签，不是身份认证。

接受候选必须提供：operation=accept、expected_revision、审核者标签、非空note和acknowledge_risks=True。必须重新回取所有Evidence；只有legacy支持时拒绝确认。更一般的语义、事件时间正确性与变更取舍由审核者负责，不自动认可模型推断。

接受步骤在一个事务中完成：

```text
检查项目/数据集 → 检查proposal.pending → 校验expected_revision
→ 校验原始Evidence与风险确认
→ 原当前版本status改superseded（正文不动）
→ 插入新的confirmed版本与decision_evidence
→ 更新当前指针与revision
→ proposal变accepted，写追加审核历史及差异处理记录
```

拒绝只更新候选审核状态、记录历史并增加topic revision，不改变当前版本。若另一方候选仍pending，差异链接仍为open；后续也审核后记录处理结果。没有自动删除相反候选。

事务使用BEGIN IMMEDIATE串行化写入，revision乐观锁要求操作人基于当前状态审核；任何错误回滚全部状态。SQLite局部唯一索引限制每个decision_id最多1条status=confirmed版本。并发审核同revision时只有1次成功，另一个RevisionConflict；想继续必须重新查看状态并明确重新审核，不自动重试绕过revision。

CLI示例（本次未在用户数据上执行）：

```powershell
python -m src.memory.decision_cli --project-id team-project create --title "请求调度方案"
python -m src.memory.decision_cli --project-id team-project propose --decision-id dec_ID --result-file investigation.json --statement "采用方案 C" --event-time 2025-02-01T00:00:00+08:00
python -m src.memory.decision_cli --project-id team-project proposal --proposal-id prop_ID
python -m src.memory.decision_cli --project-id team-project review --proposal-id prop_ID --operation accept --reviewer-label local-reviewer --expected-revision 1 --note "已核对原始来源与替代理由" --acknowledge-risks
python -m src.memory.decision_cli --project-id team-project show --decision-id dec_ID
```

默认不导出完整调查文件；可信Python调用方可显式保存result.model_dump_json后提交。显式--db/--evidence-db可以使用测试或独立存储路径；禁止LLM设置这些参数。

## 5. 时间视角与现实状态边界

本轮明确实现**系统知识时间视角**：snapshot(decision_id, scope, known_at=带时区时间)。只依据该时刻已经创建且审核的系统记录恢复当时当前指针、revision、版本、待审候选及差异状态，不按业务时间猜测确认状态。

- event_time：决策对应的业务事件时间，由明确输入提供，可为空，不以采集或审核时间冒充。
- created_at/recorded_at：主题/候选/版本进入系统的时间；版本在accept时创建，所以recorded_at与该次reviewed_at通常相同，含义不同。
- reviewed_at：可信本地审核操作实际记录的系统时间，使用服务端UTC时钟；测试注入时钟模拟历史，不开放HTTP时间覆盖。
- 默认无known_at：显示系统当前指针所指的最新已确认版本。
- 若3月补录一条业务时间为1月的版本，known_at=1月底不会显示这条未来入库/审核记录。
- 截止时刻之后发生的审核身份、版本ID、候选风险观察和冲突解决状态不会提前出现在历史快照。

没有实现独立的“仅按业务事件时间回溯”查询，也不把system_knowledge当成外部历史真相。查询一直返回world_state=unknown：可以说“系统记录的最新已确认版本是B”，不能说“现实中团队一定仍在用B”。

变化追溯用snapshot的versions、supersedes_version_id、evidence_refs、review_history和conflicts；保留B/C正文、时间、支持/反驳来源和审核备注。没有足够材料解释为什么变化时，应披露缺口，不能仅从新版本存在推断原因。

## 6. MemorySkill与ReAct接入

保持5个只读工具，不增加审核工具。MemorySkill新增query_decisions，由现有ScopedSkill适配器在search_memory中调用。只读路径不存在数据库时返回空，不建表。

Filters新增：decision_id（稳定dec_标识）、memory_view=current/history、known_at。project_id、dataset_kind、数据库路径仍禁止由模型传入。

```json
{"action":"tool_call","tool":"search_memory","query":"请求调度方案",
 "filters":{"decision_id":"dec_32位hex","memory_view":"history",
            "known_at":"2025-01-31T23:59:59+00:00"}}
```

工具结果分别返回：

- evidence：真实原始Evidence（ev_ ID/hash），来自已确认版本关联来源，受项目/数据集及过滤条件约束。
- decision_memories：record_kind=decision_version、dv_版本ID、status、event_time、recorded_at、reviewed_at、revision、supersedes_version_id、Evidence refs、system_current与world_state。内部版本不会被包装为伪造原始Evidence，也不登记进Evidence Ledger。

ToolExecutor只暴露引用均在实际返回Evidence中的内部版本；ReAct再次检查scope和Ledger引用，Observation和InvestigationResult单独保留decision_memories。AnswerAgent明确展示内部系统确认记录与现实状态未知；Workflow/Linear输出也能展示该独立区块。仍保留第三轮对外部当前有效性的保守未知策略，不能因内部记录而升级外部世界断言。

默认主题搜索复用现有BM25，但第二轮起同时索引主题标题与查询截止时点已知的版本正文；版本结果再按标题+正文相关性排序、以业务时间作平局顺序。仍可通过decision_id精确定位。`after`/`before`按决策自身的event_time过滤，event_time未知的版本在指定时间范围时被排除，绝不用recorded_at或ingested_at补日期。这不是复杂语义Decision检索。每次默认最多5条原始Evidence，无法完整携带来源的版本会被保守省略；需要更大来源集的分页/展开能力留待后续。

## 6.1 第二批：调查联动、任务覆盖与时间/冲突

第二轮在不改ReAct Runtime、Evidence Store、Ledger和数据导入主体结构的前提下补充：

- `DecisionStore.search` 支持 `view=current/history`、`known_at`、`after/before`、`decision_id`；返回 `decision_known_at`、`knowledge_cutoff`、`event_time_status`、`system_current`、`execution_status=unknown`、`world_state=unknown` 以及逐条可回取的 Evidence ID/hash/关系/来源时间/采集时间。版本正文与关联Evidence不能被读取路径改写。
- `snapshot` 增加 `timeline`：按event_time升序，未知时间排在最后，并以revision作平局；每项保留event_time、recorded_at、reviewed_at、supersedes和evidence refs。
- `src/investigation.structured_assertion_conflicts` 只比较同一事件日、同/未知环境的机器可读标量断言；不同则生成保留双方的冲突记录，不判断哪一方正确。未知事件时间不参与，因为无法确定“同一天”。
- `src/investigation_context.task_coverage` 按用户问题中的显式目标词确定需要检查的阶段（初始原因、决策形成、评审/反对、后续调整、当前或指定时点状态、未解决冲突/缺口），用确定性字面标记和已审核 Decision Memory 记录判断observed/missing。它明确标记 `semantic_completeness=unverified`，不以工具次数或Evidence数量作为完成依据；缺失显式目标时运行时可把 `sufficient` 降级为 `insufficient`。
- 权威层级分开披露：聊天按discussion级处理；meeting source按meeting_record；人工审核版本为reviewed_decision；execution仅表示出现“已部署/已执行”等字面词，不是实际执行验证。
- 重复出现的同一 Evidence（同ID+hash）在Ledger中只保留一份，`duplicate_evidence_observations` 只记录重复观察次数，不增加独立事实来源计数。

仍**未实现**：一般语义蕴含/语义完整性判定、自动判定prose矛盾、自动核验外部部署或当前现实状态、自动写Decision Memory、HTTP审核入口。`task_coverage` 是确定性存在性检查，不证明回答在语义上完整正确。

## 7. 本轮修改文件

| 文件 | 原因 |
| --- | --- |
| src/llm_smoke.py（新增） | 5个合成数据冒烟场景、授权origin闸门、实际Action与Token记录、状态区分与独占报告创建 |
| src/memory/decisions.py（新增） | 独立SQLite主题/候选/版本/审核/历史视角、幂等与风险观察 |
| src/memory/decision_cli.py（新增） | 可信本地创建/提交/查看/接受/拒绝操作，无公开HTTP写入 |
| src/skills/memory_skill.py、src/skills/registry.py | 只读Decision查询与关联原始Evidence，保留scope |
| src/investigation.py | 只读Memory过滤及独立decision_memories协议字段 |
| src/agents/react_agent.py | 读取内部版本而不伪造Evidence；支持冒烟显式禁止重定向的transport |
| src/agents/answer_agent.py | 区分内部确认状态、历史快照和现实未知；原回答入口兼容 |
| tests/test_decisions.py（新增） | 本轮26项审核/集成/授权闸门测试 |
| 本报告、README.md | 使用方式、测试结果、边界和后续工作 |

未修改EvidenceStore schema、原始Evidence数据、默认LangGraph图结构或BM25/FAISS/RRF算法。没有新增服务基础设施。

## 8. 实际测试结果与B→C示例

先跑原69项，69通过。首轮新增后86项中1个测试断言失败：同一UTC时间的Z与+00:00字符串比较不同；改为规范化时刻比较后通过，并补充后续风险/历史边界测试。最终命令：

```powershell
python -c "import faulthandler,unittest; faulthandler.dump_traceback_later(55,exit=True); result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover('tests')); raise SystemExit(not result.wasSuccessful())"
```

最终真实执行：**95 tests，95通过、0失败、0错误，7.940秒**。原69项继续通过；新增26项，其中24项Decision SQLite/工具/CLI集成，2项冒烟授权协议。Windows本地socket限制按前三轮方式获准沙箱外运行；全部模型调用仍为离线脚本或transport替身。真实LLM在线结果不计入这些数量。

生命周期测试注入系统时钟：

| 系统时间 | 操作 | 当前确认与历史 |
| --- | --- | --- |
| 2025-01-10 | 提交并人工确认B，业务事件时间2025-01-01 | B confirmed，revision=1 |
| 2025-02-10 | 提交C，业务事件时间2025-02-01，未审核 | B仍为当前；C pending |
| 2025-03-10 | 人工核对证据/风险，基于revision=1接受C | C confirmed，revision=2；B superseded，正文与证据保留 |
| 重启后读取 | 新建DecisionStore实例读取 | 指针仍为C；B/C完整历史与审核链存在 |
| 查询known_at=1月底 | 系统知识历史快照 | B confirmed；未来补录/审核不出现 |

实际终端输出DECISION_LIFECYCLE确认B=superseded、C=confirmed及C.supersedes指向B；world_state=unknown。

还验证了：无证据/错hash/非本次引用拒绝、重复候选幂等与新风险追加、相反候选保留、拒绝不改指针、SQL正文不可覆盖、唯一confirmed、双审核revision竞争、项目/fixture-live隔离、候选及冲突历史状态、supports/contradicts关系、legacy单独支持不能确认、审核中途故障回滚、重启持久化、LLM不能审核、原始Evidence ID与dv_分离、CLI只读操作、无HTTP审核路由、真实Git离线调查与原有API/CLI/SSE回归。

另外python -m compileall -q src tests api.py main.py和git diff --check通过（仅LF/CRLF提示）。本次没有向用户现有data/docs资料、数据库或索引写入测试记录；新增工程源码/测试/报告除外。测试资源均位于TemporaryDirectory并清理。

## 9. 未完成与下一阶段

1. **授权后执行真实LLM冒烟**：当前只有数据与命令准备、协议测试。在线工具选择、模型严格JSON稳定性、实际费用及任务表现仍未知；不能由离线95通过推断。
2. **服务端身份和项目授权**：目前只可信本地审核，reviewer标签不构成认证；需要服务端principal、权限和审核UI/工作流后才能开放HTTP写入。尚无签名的run持久化登记，不能把用户提交的InvestigationResult当成不可伪造执行证明。
3. **语义与时间可信度**：候选语义、Evidence支持/反驳关系、外部执行状态及替代理由仍需人工核对；没有自动判定失效或确认现实有效性。下一步可扩展业务时间视角、来源权威性及可审查的claim支持校验。

其他边界：本轮只实现决策候选/接受/拒绝/替代，未实现撤回完整流程、五类Memory全面演化、远程Issue同步或数据库迁移工具。Memory查询是一次系统读快照，不保证长调查期间没有新的人工审核；输出中的“最新”指该次查询快照。

没有破坏性操作、用户数据覆盖、索引重建、收费调用、自动Git提交或远端推送。
