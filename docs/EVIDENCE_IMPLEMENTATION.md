# 真实数据接入与统一 Evidence 实现

日期：2026-10-21。基于当前未提交工作区增量实现；未提交Git、未推送远端，未迁移或覆盖现有业务数据。第一轮的审计/修复以及原有未提交改动均保留。

## 1. 修改前确认的问题

- ingest_all覆写data/live/ingested_records.json，然后调用index_all(reset=True)；源枚举不读取live，真实数据未进入问答。
- Python hash(content)跨进程不稳定；文档仅以stem+chunk序号标识，同名不同路径会碰撞；Git只保存短SHA和仓库basename。
- Skill输出丢失记录ID和时间，ToolExecutor另构造三个字段并截断content，无法按稳定ID恢复完整内容。
- Agent/Graph/API没有显式查询范围，无法保证项目和fixture/live隔离。

## 2. 数据模型及身份规则

`src/evidence.py`使用Pydantic v2的Evidence和EvidenceScope。禁止未知字段，检验内容hash、身份hash、时区及Git定位一致性。检索分数属于hit包装，不属于Evidence事实模型。

| 字段 | 含义与约束 |
| --- | --- |
| evidence_id | ev_ + SHA-256(JSON规范身份数组)，验证与身份字段一致 |
| project_id | 显式项目标识，非空、无首尾空白、最长200字符 |
| dataset_kind | fixture/live枚举；参与身份计算及查询分区 |
| source_type | git_commit/issue/document/meeting/chat/log/code/memory/unknown |
| source_uri | 带scheme的绝对URI，无空白；新数据必填，legacy可null |
| original_record_id | 来源内部记录标识；Git完整SHA、文件相对路径、CSV明确ID或行定位；legacy缺失可null |
| event_time | 原始事件时间，有时区或null；Git用committer ISO时间；文件不以mtime或采集时间代替 |
| ingested_at | 本次采集UTC时间；legacy缺少可靠采集时间则null |
| content | 完整文本；不能空，不在持久化或ToolExecutor内截断 |
| content_hash | 原文UTF-8 SHA-256，校验必须与content一致 |
| locator | 实际取得的文件路径、完整commit_sha、repository_uri、变更文件、CSV行等；未知留空，不制造行号 |
| metadata | 作者、提交正文、CSV原始列、legacy缺失字段及未验证状态等 |

新数据身份数组：`[project_id, dataset_kind, source_type, source_uri, original_record_id]`，JSON使用固定分隔符和UTF-8。内容不参与稳定身份，修改正文只改变content_hash；不同项目、来源、路径、数据集分区不会共用ID。不使用内置hash()。

Git原始ID必须为完整40/64位SHA，locator的commit_sha及repository_uri必须匹配；新Git记录必须保留原始提交时间。默认仓库身份是本地仓库绝对file URI；移动目录/换clone会改变默认身份，可在ingest_git_history通过repository_uri显式指定长期仓库标识。本轮没有自动猜测远程仓库URL。

## 3. 存储与安全导入

默认新建data/live/evidence.sqlite3，也可通过服务配置EVIDENCE_DB_PATH或Python参数db_path指定。SQLite为标准库，不引入其他基础设施。

- evidence_observations：追加证据观察快照，保留全部旧payload，以seq确定最新观察。
- sources：项目/数据集/URI/类型的来源目录，重复插入忽略，不替换既有行。
- sync_observations：记录每次成功导入的来源及观察到的原始ID集合。当前用于同步审计，尚未实现按游标只抓新增提交。
- 整批先验证，BEGIN IMMEDIATE保护比较与追加，事务内写证据、来源与同步记录；同一证据相同内容和元数据再次采集不会追加版本（仅ingested_at变化被忽略）。并发重复导入也仅新增一条。
- 内容或元数据改变时追加新观察，不UPDATE/DELETE旧证据。可按evidence_id+content_hash读取旧正文；A→B→A回退仍保留三次观察。
- 读取使用SQLite只读模式；数据库不存在时返回空，不在查询过程中建库或重建索引。

ingest_all不再写ingested_records.json，不执行任何FAISS重建；显式reindex=True会报错。--dry-run会读取/解析和验证数据，写入数为0，不创建数据库。Git导入保存完整提交正文而非仅subject，保留完整SHA、作者、提交时间和变更文件；不自动执行git checkout、修改工作仓库或导入diff/blob历史全文。

目录导入以来源根URI+完整相对路径标识完整文本，保留原文，避免分块参数改变原始身份。同名不同路径不碰撞。只读支持的UTF-8文件类型；不自动扫描.git、虚拟环境、二进制或配置文件。CSV保存原始所有列；缺少显式ID列时采用行位置，metadata披露identity_basis=row_position，因此文件插入/重排会影响身份，应优先传id_column。TSV可通过ingest_csv的delimiter参数导入。

## 4. 完整调用链与范围隔离

```text
Git / 目录 / CSV
  → ingest_* → normalize_record → Evidence校验
  → ingest_all → EvidenceStore.append（单事务、只追加）

API ChatRequest.scope / CLI --dataset-kind --project-id
  → CoordinatorAgent.registry / ReActAgent.executor.registry
  → Graph state透传scope与服务端存储路径
  → SkillRegistry.get → ScopedSkill.run
  → retrieve_evidence
  → 按project_id + dataset_kind读取SQLite与live JSON
  → source_type过滤 → 现有BM25 → 完整Evidence hit
  → ToolExecutor保留所有字段 → LLM observation视图截断
  → AnswerAgent / ReAct结果列出evidence_id、content_hash和来源

ToolExecutor.get_evidence(id, content_hash可选)
  → Registry范围校验/本次观察缓存
  → get_evidence → 分区约束的SQLite或live JSON → 完整Evidence
```

默认scope为fixture + collaborative-memory-demo，维持原有演示Skill。live必须显式提供project_id；只写live而不写项目会校验失败（API 422）。项目边界来自运行时参数，LLM工具query不能改变范围。其他fixture项目不会自动读取demo。

live路径使用现有BM25，仅在已经过滤的证据上排序，不使用fixture实体词表、共享FAISS或任何失败后回退到demo的逻辑。HybridRetriever.retrieve/hybrid_rank新增显式scope入口；无scope的老调用仍是原fixture链路。不改变BM25、FAISS或RRF算法。

SkillRegistry的薄适配器保留run(question, top_k)接口：memory可调查当前分区所有来源；problem对应git_commit/issue/log；document对应document；dependency对应code；entrypoint对应code/document。live的dependency/entrypoint当前只检索导入的代码/文档，不执行针对其他仓库的AST解析，也不误扫当前工作区充当远程证据。

ToolExecutor不再丢弃字段或截断原始content，Agent只截断提供给模型的视图。score和score_kind=retrieval_relevance仅表示相关性；回答不以其充当事实置信度。AnswerAgent拒绝跨项目或跨dataset拼接证据，提示词和展示输出带证据ID、正文hash及来源定位信息；尚未实现逐条事实蕴含校验。

## 5. legacy兼容及旧live文件

旧Skill依旧使用原始JSON/BM25逻辑，但输出通过format_skill_result统一包装。保留source/record/id/score兼容别名；metadata保留legacy_source_label、legacy_record_locator和原始legacy_metadata。旧标签只是声称的来源，统一source_type=unknown、source_uri/original_record_id/event_time/ingested_at=null，missing_fields显式列出缺口，provenance_status=unverified_legacy；不把文件标签当作验证过的真实来源。

legacy身份使用显式标明的来源标签与记录/段落位置作为内部键；完全无定位时才用正文digest作synthetic fallback。它不具备新数据的原始记录稳定身份保证，不能声称内容变化后仍可可靠关联。

data/live/*.json里已经携带明确项目和dataset的数据可直接读取：统一Evidence先校验；旧结构使用legacy适配。没有project_id或dataset_kind的历史导出不会被自动归入任意项目。可用bind_legacy_export显式绑定：

```python
from src.evidence_store import bind_legacy_export

# 默认仅检查，不写入。先复核项目归属、记录数量与缺失来源。
plan = bind_legacy_export("data/live/ingested_records.json",
                          project_id="team-project", db_path="storage/evidence.sqlite3")
# 人工确认归属后才用dry_run=False追加，仍不改原JSON。
```

本次未对用户旧文件执行绑定。若以后执行，先把旧JSON复制到独立备份位置并校验SHA-256；已有SQLite用sqlite3.Connection.backup创建一致备份，然后dry-run核对记录数/项目后执行追加。无需迁移或删除旧文件。旧记录即使绑定到live仍标为unverified_legacy，不变成已验证Git提交；可经memory工具查询。

fixture证据可在同一个ToolExecutor/Registry生命周期内按ID取回完整旧片段及其元数据；live证据支持跨实例、进程重启后通过SQLite按ID/hash回取。fixture没有新增永久历史快照，不为此写入用户文件。

## 6. 使用方式

```powershell
python -m src.ingest --git C:/repos/team --project-id team-project --dry-run
python -m src.ingest --git C:/repos/team --project-id team-project
python main.py --react --dataset-kind live --project-id team-project
```

上述为使用示例，本次没有对实际用户仓库执行导入。自定义数据库时，导入--db和服务EVIDENCE_DB_PATH必须指向同一位置。

API请求示例：

```json
{"question":"为什么选择B，后来是否修改？","backend":"react",
 "scope":{"dataset_kind":"live","project_id":"team-project"}}
```

当前scope是逻辑过滤，不等于认证授权；API还没有principal/项目成员权限。服务端路径来自配置，不允许模型或HTTP请求传任意db_path。

## 7. 本轮文件与原因

| 文件 | 本轮作用 |
| --- | --- |
| src/evidence.py（新增） | 唯一Evidence模型、范围模型、hash和legacy适配 |
| src/evidence_store.py（新增） | 只追加SQLite、分区读取、JSON兼容、显式legacy绑定dry-run |
| src/ingest.py | 稳定身份、完整Git元数据、文件路径身份、安全追加及CLI参数 |
| src/retrieval.py | 独立live分区查询与按ID/hash回取，复用BM25 |
| src/skills/utils.py | 原有结果统一Evidence包装 |
| src/skills/memory_skill.py、problem_skill.py、document_skill.py | 保留检索记录与元数据，不先丢字段 |
| src/skills/registry.py | 绑定查询范围，分派fixture/live并检查返回分区 |
| src/agents/coordinator_agent.py、src/graph.py | 同一scope透传至每个图节点；live不预热fixture索引 |
| src/agents/react_agent.py | ToolExecutor完整字段与原文回取、模型观察视图、fallback保留范围 |
| src/agents/answer_agent.py | 输出证据ID/hash，提示词时间定位，混合分区拒绝 |
| api.py、main.py | API与CLI显式数据范围 |
| requirements.txt | 明确Pydantic>=2,<3，无新服务依赖 |
| tests/test_evidence.py（新增） | 本轮27项回归和真实Git端到端验证 |
| README.md、本文件 | 使用方式和实现边界 |

没有修改chunking算法；完整原始Evidence与旧fixture切块结果并存。本轮未修改第一轮test_regressions.py的14项测试。

## 8. 真实测试结果

完整离线回归命令（Windows本机TestClient需本地socket，已按上一轮环境原因获准沙箱外运行）：

```powershell
python -c "import faulthandler,unittest; faulthandler.dump_traceback_later(55,exit=True); result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.discover('tests')); raise SystemExit(not result.wasSuccessful())"
```

最终执行：41 tests，41通过，0失败，0错误，4.494秒。新增27项+原有14项。首轮38项运行曾有1个测试清理错误（测试自身SQLite连接未关闭），已修复为显式关闭；后续新增并发幂等、CLI和legacy标识可见性验证后全量通过。

真实Git测试类共9项，全部通过：临时仓库真实git init/commit，文档初稿→选择B→修改超时，提交时间分别为2025-01-01、02-01、03-01的10:00 +08:00，动态读取并验证三个不同完整SHA；不使用用户仓库或联网。

- 导入3条，重复导入新增0条；源SHA、时间、根提交文件列表和多行commit正文验证通过。
- live工具能够查到提交；全字段与content_hash保留；新ToolExecutor实例可按ID读取原文。
- 项目隔离、fixture/live隔离、混合回答拒绝、无匹配不回退demo通过。
- Workflow与Linear真实离线运行；ReAct实际无key fallback通过；三个API后端、SSE、CLI及导入CLI dry-run通过。
- 额外有脚本式模型驱动ReAct观察循环的离线协议测试，明确不是真实LLM结果。
- 字段校验、跨进程hash seed稳定性、并发幂等、追加版本与旧hash读取、非法整批不写入、同名不同路径、CSV列保留、旧JSON不被覆盖通过。
- python -m compileall -q api.py main.py src tests：通过；git diff --check：无空白错误，仅LF/CRLF提示。

所有测试只在TemporaryDirectory中创建仓库、JSON与SQLite；未写入既有data/docs/索引或用户资料。未执行全量索引重建、数据库迁移或数据删除。临时测试资源按测试生命周期清理。

## 9. 未完成、未验证与下一轮接口

在线验证：无。未访问真实API Key、未调用真实LLM、未下载模型、未运行FAISS在线/离线向量端到端或做性能提升实验。live主链路不依赖这些额外组件，已离线验证其可用性。

尚未建设：按Git游标的真正增量拉取（当前最多100条、可配置Python参数）、删除检测、仓库diff/blob历史内容工具、远程Issue/Chat连接器、按项目认证授权、完整API错误恢复、Memory审核演化、长文分块的统一子定位及事实引用蕴含验证。不把这些描述为已完成。

下一轮优先三项：

1. ReAct充分性/缺口/冲突判断、可恢复工具错误及预算终止；现有ToolExecutor已经提供范围固定的完整Evidence、ID/hash回取和简短模型视图，可直接接入执行轨迹。
2. 将Evidence ID绑定到答案具体结论，验证引用可取回且在当前分区，明确时序与未知部分；URI存在性校验不能代替事实支持校验。
3. 项目成员授权与按来源同步游标，补大于100条提交和目录变化场景，逐步减少重复扫描；在测试环境验证备份恢复后再接用户真实数据。
