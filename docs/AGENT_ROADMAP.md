# Agent 主导的最小增量路线

依据2026-10-21工作区审计；本文件是开发方案，不代表已实现或实验结论。产品主线保持只读自主调查，长期决策记忆为核心；不引入多Agent或新基础设施。

## 1. 第一优先：可信证据与单Agent调查闭环

先统一现有Skill返回值为Evidence：evidence_id、source_type、source_uri、original_record_id、event_time、ingested_at、content、content_hash、locator、project_id、dataset_kind（fixture/live）。ToolExecutor保留所有字段，截断只用于LLM视图，原文仍可获取。来源由工具生成；最终引用只能引用本次取得的ID，验证来源定位、项目归属、内容hash和可访问性。引用存在性校验与事实支持率分开。

复用ReActAgent与ToolExecutor，不新增角色。定义受验证action（tool、query、filters或finish），限制类型、长度和工具白名单。模型决策→执行只读工具→结构化observation→更新证据表及问题缺口→继续/结束。充分性需覆盖“原始选择原因、后续变化、当前有效性”的相应时间证据；只有初始记录时必须明确无法确认后续状态。LLM只提出充分性判断，运行时强制零证据不得事实作答、未解决冲突和缺口必须在输出披露。

工具失败返回error_code/retryable/message，不把异常当证据；一次有限重试，之后选择替代来源或带缺口结束。每次run持有deadline、最大工具调用数、最大模型轮次、累计输入/输出token；使用响应usage，缺失时估算并标识。每轮检查剩余预算，请求timeout取剩余时间与单次上限最小值。同步工具不能靠取消协程强行停止，Git等外部操作设置自身超时；不声称后台线程可被强制中断。

记录run_id、步骤、所选工具与query、结果ID、错误、耗时、token、结束原因；不记录模型隐藏思维。重复工具与相同query无新增证据则提醒或终止。结构化结束原因：sufficient/insufficient/conflict/budget/tool_failure。AnswerAgent只负责基于Evidence及缺口输出可验证结论。

验收：离线脚本模型依次选择memory→issues→docs，下一步能使用上一轮返回ID；工具失败后可换源；重复调用受限；预算耗尽有明确缺口；不存在未经工具取得的引用。通过独立benchmark后再考虑将react作为默认模式；固定Workflow保留基线。

## 2. 第二优先：先解决真实数据进入检索

将ingest和各Skill/索引的来源枚举统一，接入data/live但按project与fixture/live隔离，避免真实提交与示例历史混答。规范稳定ID：来源命名空间+原始ID，正文另存hash；Git保存完整commit SHA、仓库标识、提交时间。Markdown按来源路径与定位生成记录；CSV显式列映射，不静默丢失额外字段。

先用SQLite事务存sources/evidence/sync_cursors。按source+original_record_id幂等upsert；Git游标用commit SHA，文件以hash检测变化；删除只标记tombstone并保留历史。索引是派生缓存，以corpus版本manifest判断新旧；离线构建临时文件，验证文档与向量数量后原子发布，失败保留旧版本。本次没有运行迁移或覆盖旧文件；落地前做备份和迁移dry-run。

验收：临时目录导入真实测试Git仓库，能经工具检索到提交；重复同步不增重复；同名不同路径不冲突；单次失败保留上次数据；fixture/live可独立评测。

## 3. 第三优先：决策记忆审核与版本

最小模型：decisions（稳定主题ID、project_id）；decision_versions（version_id、revision、陈述、event_time、recorded_at、status、supersedes_version_id、reviewer）；decision_evidence（version_id、evidence_id、支持/反驳关系）；review_proposals（候选变更、理由、来源、审核状态）；conflict_links（两版本、冲突字段、审核结论）。状态区分proposed/confirmed/superseded/retracted/unknown；“过期待复核”不等于“已失效”。

LLM提议候选及矛盾，不直接覆盖原记录或判定历史决策失效。新旧证据绑定各自时间；人工确认后追加版本，保留旧版及其引用，乐观revision校验避免并发覆盖。支持按当时与当前视角查询；无后续证据返回“当前有效性未知”。来源hash相同的重复候选合并，内容冲突保留双方证据。删除先逻辑撤回，实际清理需要独立审批和保留策略。

验收：B在t1被选择、t2提出C但未审批，系统仍显示B已确认且有待审变更；t3人工确认C替代B后，能追溯所有版本；只有LLM推测不得改变B状态。不要全面实现其他四类演化再启动决策版本验证。

## 4. 后端配套

当前修复通过每请求独立Agent消除API历史串用，API暂为无状态。下一步先确定认证principal，用SQLite保存conversations/messages/runs，session必须校验owner/project；不能把任意session_id当用户身份。CLI继续独立实例。按会话串行或revision乐观锁，进程共享仅限只读检索资源；检索加载锁与索引快照生命周期单独管理。

API请求约束question长度、backend枚举及project范围；结构化响应提供answer/evidence/unresolved/termination_reason/run_id。统一异常处理：验证422、上游不可用503、超时504、内部500；外部不暴露原始异常和密钥，日志关联run_id。SSE各后端统一error/end事件并处理断连，避免重复执行；持久化任务状态后支持恢复观察。

当前规模优先HTTP线程池+受限并发，长导入先CLI作业或SQLite jobs表+单worker；需要断线后继续/失败重试时再引入简单持久任务，不依赖FastAPI BackgroundTasks保证可靠执行。不新增Redis/Celery/Kafka。

## 5. 独立Benchmark与验收

冻结测试仓库/Issue/文档/聊天快照、事件时间和hash。开发集与保留测试集按项目或主题隔离；人工标注expected_claims、gold_evidence_ids、需要的来源、历史时间关系、应披露缺口、允许的工具失败恢复。测试问题及答案不能进入被检索docs、示例prompt或mock生成器；审计/路线文档也不应当作项目历史事实。双人复核争议标签，保存标注版本。

至少7类，每类先10题，分别报告各类与宏平均，不以70题规模宣称泛化：

| 类别 | 最低任务与判定 |
| --- | --- |
| 单来源直接查询 | 明确Issue记录，正确内容且引用原ID |
| 跨来源关联 | Chat原因+Issue约束+Git实施，关联标识真实可核 |
| 多轮自主调查 | 第一工具只给线索，后续工具才能找到答案；验证轨迹 |
| 决策变化 | t1 B、t2 C，区分提议与确认及当前状态 |
| 证据冲突 | 两来源矛盾，披露双方且不擅自裁决 |
| 证据不足 | 无后续记录，拒绝确认当前有效性 |
| 工具失败 | 指定工具超时/异常，换源或说明缺失并受预算约束 |

比较Workflow与ReAct：同语料/模型/温度/预算，明确离线fallback与模型在线两种环境，记录重复运行及随机性。任务成功率由答案事实+必要引用+缺口披露共同判定；引用准确率统计被证据直接支持的引用比例，另报不存在引用率。检索指标：Hit@K=至少命中1条相关证据的查询比例；Recall@K=每题命中相关ID数/该题gold总数再宏平均；MRR=首个相关结果倒数排名再平均。无gold题不计算Recall，单独计正确拒答率，不从任务成功率分母剔除失败。

保存每次工具调用次数、端到端p50/p95延迟、输入/输出token和估算标记、失败率、结束原因；区分冷启动与热缓存。保留原始运行JSONL和环境清单，不能把关键词自动打标称为标准答案，不能将模型调用失败后关键词回退统计为纯LLM准确率。本次没有生成任何实验提升数据。
