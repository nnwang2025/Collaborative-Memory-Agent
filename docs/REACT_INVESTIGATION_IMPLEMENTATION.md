# ReAct 自主调查、可信引用与执行轨迹

日期：2026-10-22。基于当前工作区真实实现增量改造，保留此前所有未提交改动。未提交或推送Git，未迁移数据、未重建FAISS、未调用收费模型。测试只使用临时数据库及临时Git仓库。

## 1. 改造前真实入口与问题

main.py --react、api._get_agent('react')创建ReActAgent，run调用文本JSON模型、ToolExecutor.execute，再把Evidence内容缩略成Observation反馈给模型。第二轮已有Evidence身份、项目/fixture-live范围和get_evidence回取；没有重复建设EvidenceStore。

旧_parse_action仅抽取JSON对象，不验证字段、工具及长度。模型answer动作可直接返回任意summary；工具异常未统一恢复；最多5轮但没有整次deadline、工具次数或累计Token预算。单次LLM urllib超时30秒；没有单次Ledger或Trace。无Key原先回退完整Coordinator Workflow。

本轮保留ReActAgent和run(question)->str。新增run_result(question)->InvestigationResult结构化入口，API/CLI都调用同一个实现，未新增平行Agent或修改默认Workflow图结构。

## 2. 新执行链路

```text
API/CLI → ReActAgent.run_result
  → 固定EvidenceScope，创建run_id / Ledger / Budget / Trace
  → 预算预检 → 模型文本JSON → 严格Action校验
  → 重复调用检查 → ToolExecutor（有限重试、等待超时）
  → 当前项目/数据集过滤 → 现有Skill / BM25 → 完整Evidence
  → Ledger登记（ID+hash去重，保留版本）
  → Observation（新ID、来源/时间/定位、截断模型视图、缺口/冲突）
  → 模型调整query、filters或选择下一工具
  → finish申请 → AnswerAgent引用/原文摘录校验 → 运行时终止判定
  → 最终回取检查 → 结构化结果与文本回答
```

EvidenceScope取自运行时Registry，复制到单次Ledger；任何Action都不允许project_id、dataset_kind、db_path或用户身份字段。HTTP仍使用第二轮scope参数，不能传任意数据库路径。

无Key降级仍可读资料，但现在用现有Coordinator关键词路由选择1～2个工具，工具也经过同一Ledger/预算/Trace；mode明确为offline_fallback。它不再额外执行完整Workflow以生成自由总结，默认仅输出来源片段与insufficient。原有Workflow/Linear入口保持原链路，未被替换为新ReAct。

## 3. 实际协议与数据结构

所有新结构定义于src/investigation.py，Pydantic严格类型并禁止未知字段。

### Action

```json
{"action":"tool_call","tool":"search_issues","query":"调整原因",
 "filters":{"source_types":["git_commit"],"after":"2025-01-01T00:00:00+00:00"}}
```

只读白名单：search_memory/search_issues/search_docs/analyze_dependency/find_entrypoint。query非空且不超过2000字符，Action总文本不超过32000字符。filters只允许source_types、evidence_ids、after、before；分别限制枚举、数量、ID格式和带时区日期，before不得早于after。live过滤在排名前执行，不扩大原工具允许的数据类型；fixture在旧Skill候选中做交集过滤。

```json
{"action":"finish",
 "claims":[{"text":"证据正文中的逐字摘录","kind":"historical",
             "refs":[{"evidence_id":"ev_…","content_hash":"64位hash"}]}],
 "gaps":["尚未找到后续评审"],"conflicts":[],"possible_sources":["search_docs"],
 "requires_current_status":true}
```

kind允许historical/current/inference/unknown。每个claim最多1500字符/10引用，最多20个claim；gaps最多20项，每项不超过500字符；conflicts最多10组，每组至少双方引用。兼容旧action=answer（转换为finish）和tool别名，旧summary可解析但不作为已证实结论输出。只接受完整JSON或完整Markdown JSON围栏；不从任意文本中猜测并执行片段。非法动作生成protocol_error的可恢复Observation，受轮次预算限制。

### Ledger

EvidenceLedger包含run_id、固定scope、以(evidence_id, content_hash)为键的LedgerEntry、gaps和conflicts。LedgerEntry存完整Evidence快照、获取工具列表和顺序。按本次工具实际返回登记，整批身份及范围先验证再追加；模型提供的引用ID不执行登记。相同ID不同hash保留两份，并标为待核实版本差异；不是自动判断哪版有效。

entries读取返回防御性副本。Ledger不写长期库；EvidenceStore仍管理持久证据，Registry缓存现在按ID+hash保留本次取得的多版本，避免只缓存最后一版。不同run不共享Ledger，即使复用同一Agent实例也不能用旧Ledger引用绕过白名单。

### Observation

字段：tool_name、new_evidence（ID/hash）、evidence（项目、数据集、类型、来源、时间、locator、legacy标识、最多700字符视图）、error、gaps、conflicts、possible_sources。完整Evidence不因截断而改变；模型视图不能充当完整原文或登记来源。每次返回是否有新增证据，模型可根据上一轮时间或ID重新选择查询条件。

### Trace与Result

TraceStep包含step_index、action、tool_name、query、filters、evidence_ids、errors、elapsed_ms、usage、new_evidence、termination_reason。工具重试各记一个tool_attempt；最终step附结束原因。没有模型原始自由输出、隐藏思维链、密钥或环境变量。

InvestigationResult包含run_id、scope、answer、termination_reason、claims、evidence_refs、gaps、conflicts、possible_sources、trace、usage、mode及validation_level=identity_and_extract_only。API JSON保留answer/backend，增加run_id、termination_reason、evidence_refs和investigation；CLI文本展示run_id、结束原因和ID/hash。ReAct SSE增加investigation事件，再发answer与DONE；只执行一次run_result。原Workflow SSE保持原有节点进度。

Trace默认仅在内存。显式--trace-jsonl PATH才追加导出；路径由用户CLI参数提供，模型无法指定。导出每行带run_id及scope；额外排除query/filters，不保存Evidence正文、模型summary或用户完整问题。内存Trace/API包含本次查询，仍需要调用者遵守项目授权边界。

## 4. 充分性与结束原因的边界

finish只是申请。运行时校验：

- 零证据或零已通过校验的claim不能sufficient。
- 每个历史claim必须有本次Ledger内的ID/hash引用，原文可回取，项目/数据集/内容身份一致。
- 当前可自动接受的claim仅为逐字原文摘录：claim.text须出现在每条引用原文中；自由释义不作为已支持事实输出。
- legacy可作为未验证来源展示，不能据此确认历史claim。
- 模型声明缺口、发现待核实版本差异或提出双方有效引用的冲突时降级；冲突时清空已支持claims，展示双方，不裁决。
- 涉及当前状态时不允许仅以历史摘录确认当前有效性。本轮采取保守策略：对当前/仍有效等通用信号或模型current标注，一律披露未知；尚没有足够可靠的权威状态/有效期机制来自动确认“现在仍有效”。不是针对具体方案名称硬编码路径。
- 调查覆盖由模型提出，当前没有可靠的语义覆盖或事实蕴含验证；关键词信号也不能完备识别所有当前状态问题。sufficient仅表示所提摘录和引用通过这些硬性检查且未声明缺口，不是已经证明完整回答了所有自然语言子问题。结果显式标identity_and_extract_only。

| 原因 | 实现条件与验证 |
| --- | --- |
| sufficient | 有非legacy逐字历史claim、可回取引用、无缺口/冲突，预算允许；动态跨工具测试通过 |
| insufficient | 零证据、旧summary、非原文claim、无效引用、当前有效性未知、显式缺口或模型失败；各场景通过 |
| conflict | 有未解决版本差异或模型提出的双方有效引用冲突；双方引用披露测试通过 |
| budget | 轮次、工具次数、累计Token或deadline耗尽；每种预算测试通过 |
| tool_failure | 已尝试来源失败且未取得任何成功证据后申请结束；有限重试耗尽及工具超时测试通过 |

finish可直接降级结束，避免为追求sufficient无限循环。重复相同tool+trim后的query+规范化filters只执行第一次，之后反馈duplicate_call，可换工具或查询；即使上次有新增也不重复执行相同签名，严格于只拦截连续无新增。

## 5. 错误恢复与预算

ToolError：error_code、retryable、message、tool_name、elapsed_ms。临时Timeout/ConnectionError/SQLite操作错误最多重试一次（默认）；一般异常不可重试，反馈模型后可换来源。错误不成为Evidence，也不暴露异常原文。成功但空证据不被当作取得事实证据。

默认Budget：最多5模型轮次、8次实际工具尝试（包括重试）、64000累计Token、120秒整次deadline、30秒模型超时、10秒工具/回取等待、最多1次重试。可以由可信Python调用方传Budget配置；模型无权修改。

每次模型前预估消息UTF-8字节数作为保守Token上界，预留输出额度，不够则不发请求。优先记录provider的prompt_tokens/completion_tokens，缺usage或脚本模型时采用字节估算并标estimated。异常模型调用也按估算记账，不假装零成本。单次请求timeout取配置与剩余deadline的较小值，响应usage超预算或结果迟到后不再执行动作。回答阶段不额外请求LLM，避免预算之外的总结调用。

工具在单个有界daemon worker中执行，超时只停止等待，丢弃迟到结果且不登记Ledger；原同步函数可能仍运行。每个ToolExecutor只允许一个未结束worker，忙时返回tool_busy，避免无限新增线程。回取也使用剩余时间约束。Python线程没有安全的强制取消能力，当前机制不是进程隔离或硬CPU限制；urllib socket timeout也不是对任意网络栈的绝对硬实时保证。Git原有subprocess的真实10/30秒超时保留，没有新增外部命令工具。

## 6. 引用验证

AnswerAgent.compose_investigation负责claim→refs校验，Ledger.resolve只接受本次键，并调用ToolExecutor.get_evidence在固定scope下回取完整Evidence，比对完整快照（包含hash、来源、时间和项目），不存在或已改变则拒绝。最终validate_all再次核对引用，无法回取的条目不进入最终引用列表或文本证据；引用含hash，避免同ID版本混淆。

历史claim只允许逐字摘录，inference/unknown只形成缺口，current保守标未知；旧自由summary不会渲染成事实。冲突描述作为模型报告待核实，不把其文字当事实，双方Evidence正常展示。检索相关性不参与事实置信度判定。

这能阻断模型编造ID、跨run引用、跨项目/数据集引用、错hash、正文身份篡改及无引文的自由断言。不能证明原始来源本身可信、摘录没有断章取义，或不同措辞之间不存在矛盾；没有宣称消除所有幻觉。

## 7. 本轮文件

| 文件 | 原因 |
| --- | --- |
| src/investigation.py（新增） | Action/Filters/Claim/Conflict、Ledger、Observation、Budget、Trace与Result |
| src/agents/react_agent.py | 原ReAct循环增量升级，错误/超时/预算、动态观察、结构化run_result；run仍返回文本 |
| src/agents/answer_agent.py | 本次Ledger白名单与原文摘录约束、结构化回答渲染 |
| src/skills/registry.py | 可选filters透传和ID+hash缓存；默认旧接口兼容 |
| src/retrieval.py | 在现有查询分区内做可选类型/ID/时间过滤，不改排序算法 |
| api.py | JSON调查元数据和单次ReAct SSE事件 |
| main.py | 显式可选Trace导出；默认CLI入口保留 |
| tests/test_investigation.py（新增） | 本轮28项离线协议/Runtime/真实Git数据测试 |
| docs/REACT_INVESTIGATION_IMPLEMENTATION.md（新增）、README.md | 本轮报告和使用边界 |

没有修改EvidenceStore schema、历史数据、默认LangGraph图或Memory演化机制。

## 8. 测试记录与实际离线轨迹

命令（Windows TestClient需本地socket，沿用前两轮获准沙箱外离线验证方式）：

```powershell
python -c "import faulthandler,unittest; faulthandler.dump_traceback_later(55,exit=True); result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover('tests')); raise SystemExit(not result.wasSuccessful())"
```

2026-10-22最终执行：69 tests，69通过、0失败、0错误，5.704秒；原有41项全部保留并通过，新增28项。五类termination_reason均有断言。真实Workflow/Linear、API、CLI、SSE单次执行继续回归通过。另有urllib transport替身验证剩余timeout与provider usage解析；不属于真实网络验证。

本轮真实Git临时仓库创建3次提交及方案文档，实际导入SQLite，使用真实Skill和BM25；只有模型决策由脚本驱动。一次实际执行轨迹如下（ID来自临时仓库，临时资源已清理，不是可长期查询的用户证据）：

```text
run_id = 547fc76b-9bf8-4e66-9192-de2c62c2586a
1 search_memory(query=Decision, type=git_commit)
  → ev_a4fc617a0700f99a01a0369dda5602fb1d9e29407adbc1d1204b5744978f86c1 [new]
2 search_issues(query=adjustment, after=上轮event_time)
  → ev_19c371de18a5c67473e4ddcd0c2ea25b5d785c5ef1fba7f3f041dbb8505c0268 [new]
3 search_docs(query=revised)
  → ev_35eb11e1f7ed0f86119841ca9374c6c041dde98563d45e200362c06033f848b9 [new]
4 finish(claim=已取得的原文摘录, refs=对应ID+hash)
  → sufficient, validation_level=identity_and_extract_only
```

该测试验证下一轮能利用上一轮取得的时间和定位继续调查；通用源码没有固定方案或固定答案。它不是独立Benchmark，也不证明真实模型会做同样选择。

另执行python -m compileall -q api.py main.py src tests及git diff --check，均通过（Git仅提示LF/CRLF）。未改动tests以外的用户数据，未下载模型或安装新服务依赖。

## 9. 真实模型与尚未完成部分

真实LLM测试未执行。本轮未读取或输出密钥，不因环境中可能存在Key而自动调用收费模型。没有声称在线调查成功率、向量检索性能或幻觉降低比例。

剩余问题：

1. 语义充分性、结论覆盖和来源权威性仍未自动验证；当前有效性采用保守未知。自然语言矛盾主要依赖模型报告，自动检测只有同ID内容版本差异。
2. 长阻塞线程不可强杀；worker限制按Executor实例，尚无服务级并发配额、进程隔离或跨请求任务取消。请求/会话授权仍是前两轮未完成项。
3. scope是逻辑边界，不是用户身份认证；Trace默认不持久化，尚无服务端run查询/恢复接口。provider用量估算不等于账单，socket层超时不等于严格硬实时。
4. 长期Memory审核、语义版本及新旧决策取代关系均未实现；只保留本次取得的证据快照，不自动把调查结果写成团队知识。

下一轮Memory演化应复用：run_id+固定scope；claim.refs中的ID/hash；原始事件时间与来源；Ledger的版本差异、未解决缺口及冲突；termination_reason和validation_level。可以据此建立待审核候选，而不是把sufficient调查结果直接提升为已确认决策。人工确认、乐观版本校验和有效期机制仍须单独开发。
