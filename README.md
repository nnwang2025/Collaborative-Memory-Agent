# Collaborative Memory Agent

**面向研发协作历史调查与长期决策记忆的 Agent 系统（LangGraph Workflow / Linear / ReAct）**

重点业务数据是聊天、会议纪要、需求文档、评审记录和决策变更；Git Commit、Issue 属于补充证据。系统通过可回取的 Evidence ID、来源 locator、事件时间和版本记录实现跨来源追溯，而不是把项目定位成单纯的 GitHub 代码检索。

> **默认入口说明**：`python main.py` 运行的是 **fixture LangGraph Workflow 演示**，不是真实 Agent 自主调查。正式 live ReAct 调查必须显式指定 `--dataset-kind live --project-id ...`，并先通过 `src.ingest` 导入该项目的真实数据。API 默认同样是 fixture LangGraph；ReAct 需在请求中设置 `"backend": "react"` 和 live scope。

---

## 项目背景

研发团队的关键上下文分散在多个系统中：即时聊天有方案讨论，Issue 有定位过程，Commit 有修复动作，日志有故障现场，文档有架构沉淀。如果缺少统一的长期记忆系统：

- 信息分散：同一问题跨越聊天、Issue、Commit、日志和文档
- 决策难追溯：难以快速回答"当时为什么采用这个方案"
- Bug 修复经验难复用：历史修复沉淀不足，类似问题重复排查
- 文档与过程数据割裂：正式文档记录结论，聊天记录保留过程，两者难以关联

Collaborative Memory Agent 构建面向研发团队的长期记忆 Agent，让研发过程数据可被统一检索、关联、总结和追溯。

---

## 系统架构

```
用户问题
  ↓
┌──────────────────────────────────────┐
│  LangGraph StateGraph（默认 fixture 演示）│
│                                      │
│  classify_intent → route_skills ─┐   │
│       ↑                          │   │
│       │    ┌──────────────────────┘   │
│       │    ↓  [parallel fan-out]      │
│       │   memory_retrieve             │
│       │   problem_retrieve            │
│       │   dependency_retrieve         │
│       │   document_retrieve           │
│       │   entrypoint_retrieve         │
│       │    ↓  [fan-in]               │
│       └─ merge_rerank                │
│            ↓                         │
│       generate_answer                │
└──────────────────────────────────────┘
  ↓
结构化输出 [回答|证据|来源|实体|置信度|路由]
```

### 三种 Agent 模式

| 模式 | 启动方式 | 默认数据范围 | 特点 |
|------|---------|-------------|------|
| **LangGraph** (默认) | `python main.py` | fixture | StateGraph 并行 Skill fan-out，LLM 路由 |
| **ReAct** | `python main.py --react` | fixture，除非显式加 live 参数 | Think-Act-Observe 自适应循环，最多 5 步 |
| **Linear** | `python main.py --linear` | fixture | 原始顺序流水线（A/B 对比用） |

三种后端都保留。ReAct 的正式 live 用法、数据导入和 API 示例见下方“正式 live ReAct 演示”。

---

## 核心技术栈

| 技术 | 用途 |
|------|------|
| **LangGraph** | Agent 工作流引擎，并行 Skill fan-out/fan-in，状态管理 |
| **FAISS + bge-small-zh-v1.5** | fixture MemorySkill 的向量检索；**live 检索不读取该索引** |
| **BM25** (自研) | live 与 fixture 的词法检索，精确匹配关键词、文件名、Issue ID |
| **Entity Retrieval** | fixture 的实体感知检索；live 不使用共享 Entity Memory |
| **RRF** (Reciprocal Rank Fusion) | fixture 多路召回融合；live 当前为 BM25 单路 |
| **DeepSeek 官方 API（默认）** | LLM 路由分类 + Evidence → Summary 生成；可选兼容 SiliconFlow / OpenAI 兼容配置 |
| **FastAPI + SSE** | REST API + 流式输出 |
| **Docker** | 容器化部署 |

---

## 混合检索链路（fixture）与 live 检索

**fixture MemorySkill**：

```
用户查询
  ├── Dense (FAISS + bge-small-zh)  → 语义相似度
  ├── Sparse (BM25)                 → 关键词/精确匹配
  └── Entity (Entity Memory)        → 实体感知加权
                ↓
        RRF 融合排序
                ↓
           Top-K 证据
```

**live 检索**（`--dataset-kind live` / API live scope）：按 `project_id` 与 `dataset_kind` 先做来源隔离，再在 SQLite Evidence Store 上执行 BM25；支持 `source_types`、`evidence_ids`、`after`/`before`、`undated_only` 过滤。live 不使用 fixture 的 FAISS 索引、Entity Memory 或 RRF，也不会自动把不同项目的数据混在一起。

**旧演示脚本评测状态**：原脚本将至少命中一条相关文档的查询比例误标为 Recall@5，现已更名为 Hit@5。旧脚本相关性仍由关键词启发式生成，历史 73.3% / 80.0% 缺少独立标注和可复现实验依据，不作为已验证结果。详见 [代码审计](docs/CODE_AUDIT.md)。

第五轮新增 [独立 Benchmark](benchmark/README.md)：历史版本冻结 27 条合成证据、14 道测试题（七类各 2）及 2 道开发题。当前任务级评测已扩展为 **16 个任务、8 个类别**，定义与评分规则见 [任务级 Benchmark](benchmark/tasks/README.md)；两者是不同代际，历史报告不覆盖、不重标。

当前评测事实（截至本版）：

- **离线回归：243 项测试通过**（原 231 项 + 本批新增 12 项），不调用模型、不读取密钥。
- **任务级 Benchmark：16 任务 / 8 类别**。其中 **4 个不同任务**（`single-atlas`、`cross-atlas`、`conflict-atlas`、`decision-atlas`）做过真实模型在线运行；其余 12 个任务只有离线运行。
- **尚无 16 任务整体人工成功率**。`TaskSuccessRate` 和 `AppropriateAbstentionRate` 只在显式人工评分后才有值，当前保持 `null`；字面标记覆盖率不能替代语义成功判断。
- `decision-atlas` 第二轮在线结果的严格复评事实覆盖为 **1/2**。另一个事实依赖的标记出现在“未确认部分”标题中，严格判定不认可该标题作为事实证据，因此未放宽规则换取 2/2。
- 在线结果还包含协议失败和一次预算耗尽，不能据此声称 ReAct 已整体优于 Workflow。

详见 [评测报告](docs/BENCHMARK_EVALUATION.md) 与 [任务级评测说明](benchmark/tasks/README.md)。

首次 DeepSeek Flash 冒烟日志已完成[定向协议诊断与修复](docs/DEEPSEEK_PROTOCOL_FIX.md)：修正 ReAct 每轮硬编码 512 输出限制，保留停止原因并分类反馈。后续在线运行已改用修复后的配置，历史报告原样保留。

后续新增[调查完整性与上下文改进](docs/INVESTIGATION_CONTEXT_IMPLEMENTATION.md)：live 证据按查询选择可回取片段，保留父 ID/hash；日期过滤空结果可有界补查时间未知资料；请求历史去重并设独立字节预算。multi_round 按事实、来源和时间关联验收。**当前 243 项离线测试通过**；在线覆盖仍仅限上述 4 个任务。

---

## 长期记忆体系

| Memory 类型 | 职责 | 数据来源 |
|------------|------|---------|
| **Decision Memory** | 技术决策、替代方案、原因、后续计划 | 历史讨论、复盘记录；已确认版本通过 CLI 审核流程写入 `DecisionStore` |
| **Episodic Memory** | 研发过程事件和讨论片段 | 聊天记录、会议记录（导入为 Evidence，当前实现仍是静态 JSON/演示数据） |
| **Semantic Memory** | 沉淀后的项目知识 | 技术文档、架构说明（fixture 文档） |
| **Entity Memory** | 项目实体（Agent/Skill/模块/技术概念） | fixture 代码分析、文档提取 |
| **Relationship Memory** | 实体间关系（调用/依赖/路由/包含） | fixture AST 分析、架构梳理 |

> Decision Memory 的版本、审核和冲突机制已实现，但**没有自动从聊天/会议中捕获决策**；确认写入和审核目前是 CLI 流程。其余四类记忆主要是静态 JSON，不应表述为已全自动演化的生产记忆。


---

## 快速启动

### 安装

```bash
pip install -r requirements.txt
```

需要精确复现当前验证环境时，可使用同一 Python 版本安装锁定版本：

```bash
pip install -r requirements.lock
```

> `requirements.lock` 来自 Windows / Python 3.12.7 的已验证环境快照；Docker 镜像使用的 Python 3.11 与容器内安装**尚未验证**，见文末“未验证项”。

### 配置

```bash
# 复制配置模板
cp .env.example .env

# 编辑 .env，在等号后填入真实 DeepSeek 官方 API Key（模板中 Key 默认为空）
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

> - 只填写 `DEEPSEEK_API_KEY` 时，运行时会自动选择 `https://api.deepseek.com` 和 `deepseek-flash`。
> - 非空占位值（如 `your_api_key_here`、`sk-your-key-here`）会被视为未配置，不会被当作真实凭据发送。
> - 旧配置 `SILICONFLOW_API_KEY` / `SILICONFLOW_BASE_URL` / `SILICONFLOW_MODEL`、`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL_NAME` 仍然兼容；DeepSeek Key 不会被转发到其他域名。
> - 未配置 API Key 时，系统自动回退到本地模板生成（不影响离线运行和测试）。

### 运行

```bash
# 默认：fixture LangGraph Workflow 演示（不是 live 自主调查）
python main.py

# ReAct 模式；不加 live 参数时仍是 fixture
python main.py --react

# Linear 对照
python main.py --linear

# 启动 API 服务（默认 fixture LangGraph）
uvicorn api:app --reload --port 8000

# 接入真实 Git 数据
python -m src.ingest --git /path/to/your/repo --project-id your-project --dry-run
# 检查后去掉 --dry-run 执行追加导入；查询须显式选择 live 与项目
python main.py --react --dataset-kind live --project-id your-project

# 运行启发式评估（不是独立 Benchmark）
python -m src.evaluate
```

### 离线验证（不调用模型、不读取密钥）

```bash
# 243 项离线回归测试
python -m unittest discover -s tests -v

# Demo 1：校验合成 CSV 导入，不写入数据库
python examples/team_collaboration/seed_demo.py --dry-run

# Demo 2：Decision Memory 演示（默认使用新建临时目录，不写入 data/live/）
python examples/team_collaboration/decision_demo.py
```

Windows 下通过管道向交互式 CLI 输入中文时，建议先设置 `PYTHONUTF8=1`（PowerShell：`$env:PYTHONUTF8="1"`），否则 Python 可能按本地代码页解码失败。该问题属于终端输入编码，不是 Agent 运行错误。

### 正式 live ReAct 演示（可复现）

仓库自带一份**全合成、无敏感信息**的团队协作故事，覆盖需求讨论、会议决策、设计评审、修订和决策取消。它通过正式 `ingest_all` → `EvidenceStore` → live BM25 检索链路，不绕过导入写答案。

```bash
# 1) 先 dry-run 校验 CSV 映射、时间格式和记录数
python examples/team_collaboration/seed_demo.py --dry-run

# 2) 写入独立 live 数据库（默认项目 cma-team-demo）
python examples/team_collaboration/seed_demo.py \
  --project-id cma-team-demo --db data/live/team_demo.sqlite3

# 3) 正式 live ReAct CLI：显式项目 + 显式数据库
python main.py --react --dataset-kind live \
  --project-id cma-team-demo --db data/live/team_demo.sqlite3

# 4) LangGraph 对照也使用同一份 live 数据
python main.py --dataset-kind live \
  --project-id cma-team-demo --db data/live/team_demo.sqlite3

# 5) 已审核 Decision Memory 的调查演示（4 个合成案例，含时间快照与冲突披露）
python examples/team_collaboration/decision_demo.py
```

`decision_demo.py` 通过正式导入与检索取得 Evidence，再以显式本地人工审核写入两个已确认版本；它展示 `current/history`、`known_at` 快照、业务时间与采集时间区别、讨论/会议/已审核决策/部署措辞的权威层级差异，以及未裁决的结构化冲突。它不会自动确认、撤回或覆盖任何已审核决策，也不证明真实部署状态或语义完整性。

API 使用 `EVIDENCE_DB_PATH` 指向同一数据库（不要通过请求参数读取任意本地路径）：

```bash
# POSIX
export EVIDENCE_DB_PATH=data/live/team_demo.sqlite3
# PowerShell: $env:EVIDENCE_DB_PATH="data/live/team_demo.sqlite3"
uvicorn api:app --port 8000

curl -s http://localhost:8000/chat -H 'Content-Type: application/json' -d '{
  "question": "Q2 是否迁移向量数据库？请给出会议、聊天和文档证据。",
  "backend": "react",
  "scope": {"dataset_kind": "live", "project_id": "cma-team-demo"}
}'
```

API 的 `/chat` 对 ReAct 返回 `run_id`、`termination_reason`、`evidence_refs` 和 `investigation`；`/chat/stream` 对 ReAct 返回单次 investigation 事件。`termination_reason=insufficient/conflict` 表示证据或时间语义不足，不应解读为已确认结论。

### 结构化 CSV 导入格式

CSV 的 `category` 决定 `source_type`：`chat`→`chat`、`meeting`→`meeting`、`issue`→`issue`、`document`→`document`，也接受 `chats`/`minutes`/`tickets`/`docs`/`requirements`/`reviews`/`decisions` 等别名。**CLI 的 `--csv` 和 Python `ingest_csv` 都必须显式给 category，不存在把所有行静默写成 document 的默认值。**

```bash
python -m src.ingest \
  --csv examples/team_collaboration/meetings.csv \
  --category meeting --content-column content --id-column meeting_id \
  --time-column event_time --sender-column chair --channel-column channel \
  --subject-column topic --source-ref-column source_ref \
  --project-id cma-team-demo --db data/live/team_demo.sqlite3
```

`event_time` 必须带时区（如 `2026-01-15T14:00:00+08:00`）；空白表示未知，非空但缺时区会被拒绝而不是猜测。任何引用未记录 `event_time` 的结构化记录而形成的结论，都会标记“事件时间未知”并按 `insufficient` 处理，不会把缺失时间推断成确定日期。Python 调用可为每个 CSV 传 dict 规格：

```python
from src.ingest import ingest_all
ingest_all(csv_files=[{
    "path": "examples/team_collaboration/chats.csv", "category": "chat",
    "content_column": "content", "id_column": "message_id",
    "time_column": "event_time", "sender_column": "sender",
    "channel_column": "channel", "subject_column": "subject",
    "source_ref_column": "source_ref",
}], project_id="cma-team-demo", db_path="data/live/team_demo.sqlite3")
```

可选的 `assertions_column` 指向一个 JSON 对象列，值只能是字符串、数字或布尔值。它只用于同一事件日的机器可读断言差异检测，保留双方来源，不判断哪一方正确，也不代表实际执行状态。


---

## 示例问答（fixture/演示输出，不是 live 调查结论）

以下示例用于展示输出结构；默认 fixture Workflow 的答案不能当作真实项目历史事实。正式项目应使用上面的 live 导入 + live scope。

### 为什么采用混合检索而不是纯向量检索？

```
【回答】(LangGraph · 2 skills: document, memory)
检索层采用 Exact Match、BM25、Entity Retrieval、Embedding Retrieval 和 Rerank 的混合检索。

采用该方案的原因：
1. 研发问题同时包含精确实体、文件名、函数名、Issue ID 和模糊自然语言
2. BM25 适合短文本和关键词召回
3. Embedding Retrieval 适合模糊语义查询
4. Rerank 综合实体重合度、来源可信度和上下文相关性重排

相比纯 Embedding Retrieval、单一 BM25、只靠关键词搜索：
更适合研发协作场景中的实体+语义混合问题。
```

### MemorySkill 是怎么被调用的？

```
【回答】(ReAct · 2步 · search_memory, analyze_dependency)
调用路径：main.py 创建 CoordinatorAgent
→ CoordinatorAgent.ask(question)
→ route_scores(question) + route(question)
→ SkillRegistry.get('memory') 取得 MemorySkill 实例
→ MemorySkill.run(question) 完成长期记忆检索
```

### Rerank 相关问题怎么修复？

```
【回答】
问题现象：检索结果混入无关聊天或低相关片段
根因：多路召回后缺少明确的重排和证据聚焦
修复方式：提高 Decision Memory 和实体命中权重，用 Rerank 聚合证据
涉及文件：src/agents/answer_agent.py、src/skills/memory_skill.py
```

---

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/chat` | 问答（JSON）；请求体含 `question`、`backend`、`scope` |
| `POST` | `/chat/stream` | 问答（SSE 流式） |
| `GET` | `/health` | 健康检查 |

启动后访问 `http://localhost:8000/docs` 查看完整 Swagger 文档。API 默认 `backend=langgraph` 且 scope 为 fixture；live ReAct 必须显式传 `backend=react` 与 live scope，并通过 `EVIDENCE_DB_PATH` 选择数据库。当前 API 没有认证和限流，只适合本地/受控环境，不能直接暴露到公网。

---

## 项目结构

```
├── main.py                  # CLI 入口（支持 --langgraph/--react/--linear）
├── api.py                   # FastAPI 服务
├── Dockerfile / docker-compose.yml / .dockerignore
├── requirements.txt / requirements.lock
├── .env.example             # 模板；真实 .env 不进入镜像
│
├── src/
│   ├── graph.py             # LangGraph StateGraph 工作流定义
│   ├── retrieval.py         # fixture 混合检索；live 为隔离后的 BM25
│   ├── ingest.py            # Git/目录/结构化 CSV 导入
│   ├── evidence.py          # Evidence 身份、scope、时间、hash
│   ├── evidence_store.py    # append-only SQLite 证据存储
│   ├── investigation.py     # ReAct 协议与 Evidence Ledger
│   ├── investigation_context.py # 证据片段、上下文预算、来源覆盖
│   ├── llm_config.py        # 统一模型配置解析（DeepSeek 优先，兼容旧变量）
│   ├── agents/
│   │   ├── coordinator_agent.py  # 主 Agent (LLM 路由 + 对话记忆)
│   │   ├── react_agent.py       # ReAct Agent (Think-Act-Observe)
│   │   └── answer_agent.py      # 回答合成
│   ├── skills/
│   │   ├── registry.py          # Skill 注册中心
│   │   ├── memory_skill.py      # 长期记忆检索
│   │   ├── problem_skill.py     # Issue/Bug/日志检索
│   │   ├── dependency_skill.py  # AST 代码分析 + 调用链
│   │   ├── document_skill.py    # 文档/方案检索
│   │   ├── entrypoint_skill.py  # 入口/启动流程检索
│   │   └── utils.py             # 公共工具 (BM25, 关键词, 实体抽取)
│   ├── memory/
│   │   └── conversation.py      # 多轮对话记忆
│   └── evaluate.py              # 启发式评估 (Hit@5 + 路由命中率)
│
├── examples/team_collaboration/ # 全合成聊天/会议/评审/决策 CSV 与 seed 脚本
├── data/                    # fixture 数据；data/live/ 为本地 live 导入（不入镜像）
│   ├── decision_memory.json
│   ├── memory_records.json
│   ├── episodic_summaries.json
│   ├── entity_memory.json
│   ├── relationship_memory.json
│   ├── chats/ / issues/ / commits/ / logs/
│   └── live/                # 真实数据 (git ingest 输出)
│
├── docs/                    # 技术文档 (RAG 源)
│   ├── architecture.md
│   ├── langgraph-workflow.md
│   ├── memory-design.md
│   └── retrieval-design.md
│
└── db_md/                   # FAISS 向量索引
    ├── faiss.index
    └── docs.pkl
```

---

## 项目亮点

- **LangGraph 工作流**：显式 StateGraph，并行 Skill fan-out，可观测可调试（默认 fixture）
- **Hybrid RAG（fixture）**：Dense + Sparse + Entity 三路检索，RRF 融合；live 检索是隔离后的 BM25
- **ReAct Agent**：自适应 Think-Act-Observe 循环，动态选择工具和查询策略；可显式切到 live
- **LLM 路由 + 关键词 Fallback**：既利用 LLM 语义理解，又保证离线可用
- **长期记忆分层**：Decision/Episodic/Semantic/Entity/Relationship 结构；其中 Decision 有版本/审核 CLI，其余仍以静态数据为主
- **证据化输出**：回答+证据+来源+实体+置信度+路由；live ReAct 额外校验 Evidence ID、hash、scope 和原文子串
- **多轮对话记忆**：追问感知，窗口滑动 + 历史摘要
- **双模式部署**：CLI 交互 + FastAPI 流式服务 + Docker 容器化
- **离线 Benchmark/回归**：冻结合成集、任务级定义与手写评分入口；243 项离线测试通过。真实模型仅覆盖 4 个任务，整体人工成功率尚不存在。

## Docker 部署与安全边界

- 构建上下文由 `.dockerignore` 排除 `.env*`（保留 `.env.example`）、私钥、`venv/`、缓存、`data/live/`、SQLite/FAISS 索引、`benchmark/reports/`、测试和临时报告。
- Dockerfile 在 `COPY . .` 后执行 `test ! -e .env ...`，敏感文件意外进入上下文时构建失败。
- 容器以非 root 的 `10001:10001` 运行；compose 额外启用 `no-new-privileges` 并丢弃全部 capabilities。
- `data/` 通过运行时 volume 挂载，不烘焙进镜像；`db_md` 使用命名 volume 保存构建期 fixture 索引。若要在容器内写入 `data/live/`，需让宿主机目录对 UID 10001 可写；更简单的做法是先在宿主机运行 seed 脚本，再启动容器。
- 构建期 fixture 索引失败不会再静默：会打印警告并继续，live 检索不依赖该索引。
- **未验证项**：当前环境没有 Docker CLI，未实际执行 `docker build`/`docker compose config`；Dockerfile/compose 中的 Python 3.11 依赖安装与容器内运行均未验证。上述仅为静态配置修改。

## 未实现或未验证的能力

以下内容**不能**表述为已完成：

- **外部 SaaS 接入**：没有实现飞书、Slack、Jira、Confluence 等外部 SaaS 的在线导入/同步；当前入口是 Git、目录和结构化 CSV。
- **完整增量同步**：没有通用的全量对账、删除传播或外部游标增量同步；重复导入依靠 Evidence 内容哈希去重。
- **Docker 实际构建**：本机无 Docker CLI，镜像构建与容器运行未验证。
- **16 任务整体在线成功率**：只有 4 个任务有真实模型运行记录，且没有对全部任务做人工评分。
- **连续会话持久化**：API 为请求独立实例，没有跨请求的持久会话。

## 工程审计与后续开发

当前默认模式为固定 LangGraph Workflow，可选 ReAct 执行动态调查。五类长期记忆主要为静态 JSON；Decision Memory 有不可变版本、提案/审核和冲突记录，写入/审核仍是 CLI。只读查询已支持标题+版本正文、`current/history`、`known_at` 和业务时间范围；任务覆盖是确定性的目标/字面标记存在性检查，明确不声称语义完整性。SSE 提供节点进度、最终答案和结构化错误事件；任一后端异常后都会发送终止标记 `data: [DONE]`。

- [代码审计与修复验证](docs/CODE_AUDIT.md)
- [Agent 主导增量路线](docs/AGENT_ROADMAP.md)
- [真实证据接入与 Evidence 实现](docs/EVIDENCE_IMPLEMENTATION.md)
- [ReAct 调查、引用与执行轨迹](docs/REACT_INVESTIGATION_IMPLEMENTATION.md)
- [Decision Memory 审核与 LLM 冒烟准备](docs/DECISION_MEMORY_IMPLEMENTATION.md)
- 结构化接入说明：[examples/team_collaboration/README.md](examples/team_collaboration/README.md)
- 离线回归：`python -m unittest discover -s tests -v`，测试依赖见 `requirements-dev.txt`。新增结构化导入测试不调用模型、不读取密钥。
