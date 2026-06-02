# Collaborative Memory Agent

**基于 LangGraph + Hybrid RAG 的研发协作长期记忆 Agent 系统**

面向研发协作场景，整合聊天记录、Issue、Commit、日志和技术文档等多源过程数据，通过 Agent + RAG 实现历史决策回溯、问题排查复盘、调用链分析和证据化问答。

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
│  LangGraph StateGraph (工作流引擎)     │
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

| 模式 | 启动方式 | 特点 |
|------|---------|------|
| **LangGraph** (默认) | `python main.py` | StateGraph 并行 Skill fan-out，LLM 路由 |
| **ReAct** | `python main.py --react` | Think-Act-Observe 自适应循环，最多 5 步 |
| **Linear** | `python main.py --linear` | 原始顺序流水线（A/B 对比用） |

---

## 核心技术栈

| 技术 | 用途 |
|------|------|
| **LangGraph** | Agent 工作流引擎，并行 Skill fan-out/fan-in，状态管理 |
| **FAISS + bge-small-zh-v1.5** | 向量检索引擎，512 维语义嵌入 |
| **BM25** (自研) | 词法检索，精确匹配关键词、文件名、Issue ID |
| **Entity Retrieval** | 实体感知检索，基于 Entity Memory 提升相关权重 |
| **RRF** (Reciprocal Rank Fusion) | 多路召回融合算法，合并 Dense + Sparse + Entity 结果 |
| **Qwen2.5-14B** (SiliconFlow) | LLM 路由分类 + Evidence → Summary 生成 |
| **FastAPI + SSE** | REST API + 流式输出 |
| **Docker** | 容器化部署 |

---

## 混合检索链路

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

**消融实验结果** (20 条测试集):

| 策略 | Recall@5 |
|------|----------|
| BM25-only | 73.3% |
| Hybrid (Dense + Sparse + Entity) | **80.0%** (+6.7%) |

---

## 长期记忆体系

| Memory 类型 | 职责 | 数据来源 |
|------------|------|---------|
| **Decision Memory** | 技术决策、替代方案、原因、后续计划 | 历史讨论、复盘记录 |
| **Episodic Memory** | 研发过程事件和讨论片段 | 聊天记录、会议记录 |
| **Semantic Memory** | 沉淀后的项目知识 | 技术文档、架构说明 |
| **Entity Memory** | 项目实体（Agent/Skill/模块/技术概念） | 代码分析、文档提取 |
| **Relationship Memory** | 实体间关系（调用/依赖/路由/包含） | AST 分析、架构梳理 |

---

## 快速启动

### 安装

```bash
pip install -r requirements.txt
```

### 配置

```bash
# 复制配置模板
cp .env.example .env

# 编辑 .env，填入 SiliconFlow API Key
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.siliconflow.cn/v1
MODEL_NAME=Qwen/Qwen2.5-14B-Instruct
```

> 未配置 API Key 时，系统自动回退到本地模板生成（不影响运行）。

### 运行

```bash
# CLI 模式（LangGraph）
python main.py

# ReAct Agent 模式
python main.py --react

# 启动 API 服务
uvicorn api:app --reload --port 8000

# 接入真实 Git 数据
python -m src.ingest --git /path/to/your/repo

# 运行评估基准
python -m src.evaluate
```

---

## 示例问答

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
| `POST` | `/chat` | 问答（JSON） |
| `POST` | `/chat/stream` | 问答（SSE 流式） |
| `GET` | `/health` | 健康检查 |

启动后访问 `http://localhost:8000/docs` 查看完整 Swagger 文档。

---

## 项目结构

```
├── main.py                  # CLI 入口（支持 --langgraph/--react/--linear）
├── api.py                   # FastAPI 服务
├── Dockerfile / docker-compose.yml
├── requirements.txt
├── .env / .env.example
│
├── src/
│   ├── graph.py             # LangGraph StateGraph 工作流定义
│   ├── retrieval.py         # 混合检索引擎 (FAISS + BM25 + Entity + RRF)
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
│   ├── ingest.py                # 真实数据接入 (Git)
│   └── evaluate.py              # 评估基准 (Recall@5 + 路由准确率)
│
├── data/                    # 数据文件 (mock + live)
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

- **LangGraph 工作流**：显式 StateGraph，并行 Skill fan-out，可观测可调试
- **Hybrid RAG**：Dense + Sparse + Entity 三路检索，RRF 融合，Recall@5 达 80%
- **ReAct Agent**：自适应 Think-Act-Observe 循环，动态选择工具和查询策略
- **LLM 路由 + 关键词 Fallback**：既利用 LLM 语义理解，又保证离线可用
- **五层长期记忆**：Decision/Episodic/Semantic/Entity/Relationship 结构化管理研发知识
- **证据化输出**：回答+证据+来源+实体+置信度+路由，可追溯可验证
- **多轮对话记忆**：追问感知，窗口滑动 + 历史摘要
- **双模式部署**：CLI 交互 + FastAPI 流式服务 + Docker 容器化
- **消融实验**：量化验证混合检索 vs 纯 BM25、LLM 路由 vs 关键词的效果差异
