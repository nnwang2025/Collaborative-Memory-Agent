# Collaborative Memory Agent 代码审计

审计日期：2026-10-21。依据当前工作区真实代码，不以 README 的能力描述代替实现证据。工作区原有 `src/ingest.py`、`src/retrieval.py` 修改以及未跟踪的 `src/chunking.py`、`INTERVIEW_HANDBOOK.md` 均保留；本次修复在其基础上进行。未提交 Git、未修改远端、未执行导入、索引重建、数据删除或迁移。

## 1. 真实架构与调用链

CLI `main.main` 默认 `CoordinatorAgent(use_langgraph=True).ask`；API `ChatRequest.backend` 默认也是 `langgraph`。核心默认模式是固定的一轮路由检索 Workflow，不具有证据不足后回到调查节点的边。

```text
main.run_simple_loop / api.chat
  → CoordinatorAgent.ask
  → ConversationMemory.enrich_question（CLI实例内）
  → _ask_with_graph → get_graph().invoke
  → classify_intent_node → CoordinatorAgent.route_scores
  → route_skills_node → CoordinatorAgent.route（再次计算分数）
  → 条件选择1～2个Skill并行运行
  → merge_rerank_node → AnswerAgent._top_relevant
  → generate_answer_node → AnswerAgent.compose → END
```

State 包含 question/scores/skill_names/skill_results/ranked_evidence/answer；skill_results 用浅合并 reducer 收集并行结果。没有 checkpointer、thread_id 或数据库。`generate_answer_node` 不消费 ranked_evidence，而在 compose 内重新排序。

可选 `--linear`：`_ask_linear → route_scores + route → Skill.run → compose`。

可选 `--react` / API react：`ReActAgent.run → _call_llm → _parse_action → ToolExecutor.execute → Skill.run → observation → 下一次LLM决策`。五个工具映射到五个本地 Skill；属于文本 JSON 动态工具选择，不是原生 function calling。模型可修改下一轮 query 或换工具；上限是5个模型决策循环（不是保证5次工具调用）。无密钥时回退到默认 Workflow。run 内消息是局部变量，没有跨轮长期对话历史。

工具与证据：

| 模块 | 真实来源及路径 |
| --- | --- |
| MemorySkill | decision_memory、memory_records、episodic_summaries、data/chats；优先 hybrid_rank，失败后本地 BM25/实体/关键词 |
| ProblemSkill | data/issues、logs、commits 的静态 JSON；本地 BM25/实体/关键词 |
| DocumentSkill | docs Markdown 分段，加实体相关 decision/issue/commit JSON；本地 BM25 |
| DependencySkill | 本地 Python AST 导入/符号/调用扫描；MemorySkill 调用链另有硬编码特例 |
| EntrypointSkill | 扫描 main/app/server/cli 和 src；候选列表漏掉 api.py |
| retrieval | _build_documents → 分类切块 → bge-small嵌入 → FAISS；BM25 + Entity → RRF；没有 cross-encoder 调用 |
| 回答 | AnswerAgent.compose 规则重排，SiliconFlow总结或离线降级；ReAct可直接返回模型summary |

## 2. 已实现与缺口

| 能力 | 代码事实与局限 |
| --- | --- |
| 动态调查 | ReAct已具备跨工具多轮循环；默认 Workflow 无调查回路；证据充分性仅提示词判断 |
| 失败恢复/预算 | 模型请求有15/30秒超时，ReAct有循环上限；工具异常未转为可恢复 observation；无整次deadline、累计token预算、结构化轨迹 |
| Decision Memory | 4条静态记录，id/title/decision/alternatives/reason/future_plan/entities/source_files；无版本、有效状态、审批和冲突检测 |
| Semantic Memory | 12条静态知识；build_memory_records可按聊天滑窗生成记录，但不在问答链路中写入 |
| Episodic Memory | 12条静态事件摘要；只读检索 |
| Entity Memory | 16条实体/别名，驱动词表匹配及排序，不是模型NER |
| Relationship Memory | 8条静态关系，可被全库索引；没有关系图遍历工具或冲突演化 |
| Memory管理 | cold_memory_files只是路径常量；没有Memory Manager、运行时CRUD、自动提取或审核写入 |
| Git/Issue/Chat/Log/Docs | Git确有subprocess git log导入；目录/CSV也能导入；Issue/Chat/Log仅本地数据，无远程连接器 |
| 持久化 | JSON及FAISS/pickle文件；无业务数据库、缓存、会话持久化或恢复 |
| API | JSON和SSE节点进度，非逐token流；无身份认证、用户归属、授权或会话ID |

所有五类数据及文档主要由 `data/generate_collaborative_memory_mock_data.py` 的同一套手工叙事生成；源文件关联是文件级列表，不是可验证的原始记录引用。当前检查 decision 的 source_files 均存在，不能据此宣称引用语义正确。许多 Skill 输出丢失 record/paragraph，ToolExecutor进一步丢弃 metadata、时间和ID；跨时间追溯仍不可靠。

## 3. 已确认问题与优先级

以下代码证据以文件和函数为稳定定位；“已修复”描述的是本次变更后的状态。

| ID/级别 | 文件/函数及原代码证据 | 影响 | 最小方案/状态 |
| --- | --- | --- | --- |
| A01 P0 | api._get_agent：`_agents[backend]`；CoordinatorAgent.ask：enrich_question/add | 所有用户同后端共享上次问答，追问可能泄露上下文 | 已改为请求独立实例；未认证前API按无状态处理 |
| A02 P0 | CoordinatorAgent.astream：`for chunk in graph.astream`；api.chat_stream后续再ask | async_generator不可同步迭代，SSE失败；修好迭代后仍重复推理 | 已用async def/async for，直接使用generate_answer结果，只执行一次 |
| A03 P0 | evaluate.evaluate_retrieval：相交非空计1，除以查询数，却命名Recall | 指标口径错误，宣传数据失真 | 已改Hit@5；保留启发式标签并显式披露skipped_queries/label_method |
| A04 P0 | retrieval.hybrid_rank：全库dense与subset混合；RRF缺失id时以排名为ID | 混入非目标记忆，排名相同的不同记录被合并 | 已限定subset的dense候选为原文精确匹配，并给legacy记录确定性ID；空subset返回空 |
| A05 P0 | AnswerAgent._fallback_fix_summary等按topic硬编码事实；无evidence仍生成结论 | 无证据断言修复、人物、优化，路由分数被当事实置信度 | 已让主_summary路径无证据拒答，离线有证据仅引用片段；来源只来自展示证据；置信度标未校准 |
| A06 P0 | chunking.RecursiveSplitter.split：split后重新附上separator并递归同一输入 | 长文本以分隔符开头时可能无限递归；非法overlap可导致force_split不前进 | 已跳过无进展分割，构造时校验参数 |
| A07 P0 | requirements未列fastapi/uvicorn/sentence-transformers/faiss-cpu；Docker CMD uvicorn | 按清单安装不能可靠启动声明的API/向量链路 | 已补运行依赖；完整干净安装未验证 |
| A08 P0 | ReActAgent._parse_action可返回list/null；run直接.get；answer可在零证据时返回 | 模型合法JSON但非object导致崩溃，或未调查就生成事实 | 已拒绝非object JSON；零证据answer降级为无法确认 |
| A09 P1 | graph.classify_intent_node/route_skills_node各new Coordinator；route再算scores | 分类与路由重复LLM调用，state分数可能与实际选择不一致；传入use_llm_routing=False不传至节点 | 后续注入同一配置，route消费已有scores |
| A10 P1 | ingest_all写data/live/ingested_records.json；_iter_data_sources不读live | 真实导入数据未进入检索，即使重建索引仍读旧mock | 后续统一数据源枚举与来源隔离；本次不执行写入/覆盖 |
| A11 P1 | ingest.normalize_record使用hash(content)%10000000；ingest_directory以stem编号；ingest_all直接write_text | ID跨进程不稳定、路径同名碰撞、再次导入覆盖旧数据；无增量游标 | 内容hash+来源命名空间、事务upsert和同步游标；本次不迁移旧数据 |
| A12 P1 | ToolExecutor.execute无try/except；只输出source/content/score且截断300字符 | 单工具失败终止调查；身份时间丢失；无法核对完整引用 | 统一Evidence与失败observation，保留ID、时间及精确定位 |
| A13 P1 | DependencySkill._memory_skill_chain硬编码；_call_chain_docs特例直接返回 | 标称AST证据却返回历史模板，且调用关系与当前graph默认链不一致 | 删除特例或标注静态提示，以真实AST行号输出 |
| A14 P1 | API无用户认证和持久化；ConversationMemory在进程内；astar无持久化 | API暂无连续会话能力，重启不可恢复 | 下一阶段加SQLite及认证所有者校验，不能仅信任客户端session_id |
| A15 P1 | AnswerAgent只读SILICONFLOW_*，Coordinator/ReAct兼容OPENAI_* | README模板仅OPENAI_*时路由可在线但最终总结离线 | 统一配置读取和明确降级状态 |
| A16 P2 | graph.merge_rerank输出未消费；多处BM25/JSON加载；反复创建registry | 重复计算/死中间状态；规则非模型rerank | 复用配置和排序结果，暂不大规模重构 |
| A17 P2 | LogChunker时间匹配后pass；ConversationChunker把regex传str.split | 没有真正5分钟窗口，speaker正则不会执行 | 明确按大小切块或实现实际解析 |

修复A04采用保守文本精确匹配：长记录与dense分块内容不一致时可能失去dense增益，但不会凭近似匹配把外部记录冒充子集证据。后续用统一稳定evidence_id替代。

## 4. 待验证风险与边界

- FAISSIndexer.load使用pickle.load；若攻击者能替换索引文件则有反序列化风险。未发现网络上传索引入口，不能断言存在可远程利用漏洞。
- _shared/加载状态缺少完整锁；预热不保证并发请求安全；index_all(reset=True)先删旧索引，重建失败可能丢失缓存。没有执行该操作。
- 生成脚本会覆盖现有data和docs，ingest_all也覆盖输出；此次仅审查，没有运行，未发生用户资料覆盖。
- CORS开放且API无认证，不适合直接暴露团队私有知识。未确认当前部署是否公网可达。
- 检查受版本管理的源码未发现硬编码真实密钥；.env在ignore且未被Git跟踪。本次未输出私密配置，不能推断历史提交或外部部署无泄露。
- 提示词要求基于证据不等于逐条蕴含校验；ReAct已有证据时仍可能生成不受支持的summary，来源存在性不证明事实正确。

## 5. 测试与评测事实

基线：`python -m unittest discover -v`：0 tests，NO TESTS RAN；仓库没有原有自动化测试。26个项目Python文件通过AST语法解析。

环境已有fastapi、httpx、langgraph、numpy；缺pytest、sentence_transformers、faiss。使用unittest新增离线回归，避免真实网络、私密密钥、索引重建和数据覆盖。最终命令及执行结果见本文末尾验证记录。

原evaluate仅20条项目自造查询；相关文档由“至少2个关键词命中”自动打标，没有人工gold evidence ID、独立标准答案、外部保留集、MRR、真正Recall、答案事实正确性或端到端成功率。无相关文档的查询会跳过，分母变小。本次显式报告跳过数，仍不可当独立benchmark。LLM路由失败回退关键词，原llm_accuracy不是纯LLM结果，已重命名llm_or_keyword_fallback_accuracy。

README的73.3%/80.0%及INTERVIEW_HANDBOOK中重复数据缺少固定语料快照、模型版本、运行日志和独立标注；更不能当Recall@5。未复现实验，不提供替代数值。延迟脚本声称禁用LLM路由但graph节点创建默认Coordinator，标签也不可信。

## 6. 宣传描述校正

可保留：有固定LangGraph图、可选文本ReAct、多来源本地检索、RRF、CLI/API入口、JSON与FAISS文件持久化。

需要限定：五类记忆是静态数据组织；动态ReAct只在选定模式且模型可用时生效；SSE是节点进度；Rerank是规则；实体是词表；多轮记忆仅CLI实例内。

缺乏实现或实验支撑：自主长期Memory演化、决策版本和有效性、真实研发系统跨来源增量同步、模型Reranker、默认多轮自主调查、团队会话持久化、80% Recall与LLM相对关键词提升。旧docs/langgraph-workflow仍把已实现图写成未来规划；生成器还会再覆盖这些说明。后续应把项目文档与可检索示例证据分开版本管理。

## 7. 最终验证记录

- 初次受限环境运行 `python -m unittest discover -s tests -v` 阻塞；faulthandler的12秒诊断定位为Windows asyncio事件循环创建socketpair时阻塞在socket.accept，尚未进入应用。已终止该运行。
- 获准在沙箱外运行同一离线测试集：`python -c "import faulthandler,unittest; faulthandler.dump_traceback_later(45,exit=True); result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover('tests')); raise SystemExit(not result.wasSuccessful())"`：14通过、0失败、0错误，1.438秒。
- 覆盖：并发API请求上下文隔离、实例隔离、请求校验、三个后端API分派、真实Coordinator异步stream只运行一次、真实LangGraph离线路由/检索/回答、空证据拒答、离线不编造修复、ReAct非法JSON和无证据回答、子集检索与ID不合并、切块递归终止及参数、Hit指标口径。
- API分派/SSE使用替身隔离模型与图；另有真实图离线集成测试，但没有真实LLM或向量端到端测试，不能据此宣称在线Agent任务成功率。
- `python -c "from src.retrieval import HybridRetriever; r=HybridRetriever(); r._ensure_loaded()"`：失败，ModuleNotFoundError: sentence_transformers；因此未运行完整向量benchmark，也未声称FAISS及模型在线链路通过。faiss同样未安装。没有下载依赖或模型。
- `git diff --check`：无空白错误（仅Git的LF/CRLF提示）。新增测试依赖见requirements-dev.txt；运行依赖已补充但未执行全新环境安装或Docker构建。
