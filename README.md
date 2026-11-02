# Collaborative Memory Agent

**面向研发协作历史调查与长期决策记忆的 Agent 系统（LangGraph Workflow / Linear / ReAct）**

重点业务数据是聊天、会议纪要、需求文档、评审记录和决策变更；Git Commit、Issue 属于补充证据。系统通过可回取的 Evidence ID、来源 locator、事件时间和版本记录实现跨来源追溯，而不是把项目定位成单纯的 GitHub 代码检索。

> **默认入口说明**：`python main.py` 运行的是 **fixture LangGraph Workflow 演示**，不是真实 Agent 自主调查。正式 live ReAct 调查必须显式指定 `--dataset-kind live --project-id ...`，并先通过 `src.ingest` 或 V2 `v2-source-add` / `v2-sync` 导入该项目的真实数据。兼容 API `/chat` 默认同样是 fixture LangGraph；ReAct 需在请求中设置 `"backend": "react"` 和 live scope。
>
> **Web 工作台**：启动 API 后访问 `http://localhost:8000/demo/`。它调用持久化 `/sessions`、`/runs` 运行时（ReAct + SSE），不是 `/chat` 兼容接口。未配置模型凭据时会记录 `model_skipped`（`no_credential` / `offline_fallback`），通常以 `insufficient` 结束；这只能用于验证调查链、SSE、Evidence 与 Trace，不代表真实模型质量。

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

Phase 2 在 ReAct 之上增加了持久化 Session/Run 运行时（SQLite 事件流、提问幂等、协作式取消/超时、重启中断标记、Session 追问上下文与 SSE 断线重放）。详见 [docs/AGENT_RUNTIME_PHASE2.md](docs/AGENT_RUNTIME_PHASE2.md)。

Phase 3 增加确定性的 Context Engineering：模型感知 token 预算、版本化调查状态、优先级证据选择与去重、分层压缩、结构化 `context_limit`、每轮 Context Trace 与 Run 查询接口。原始 Evidence、检索阈值、ReAct 工具协议和 Run/Session 生命周期保持不变。详见 [docs/AGENT_RUNTIME_PHASE3.md](docs/AGENT_RUNTIME_PHASE3.md)。

---

## 核心技术栈

| 技术 | 用途 |
|------|------|
| **LangGraph** | Agent 工作流引擎，并行 Skill fan-out/fan-in，状态管理 |
| **FAISS + bge-small-zh-v1.5** | fixture MemorySkill 与 V2 live chunk 索引的向量检索；两者索引独立 |
| **BM25** (自研) | fixture 与 live 的词法检索，精确匹配关键词、文件名、Issue ID |
| **Entity Retrieval** | fixture 的实体感知检索；live 不使用共享 Entity Memory |
| **RRF** (Reciprocal Rank Fusion) | fixture 多路召回融合；V2 live 支持 BM25+Dense RRF，也可单路运行 |
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

**live 检索**（`--dataset-kind live` / API live scope）：按 `project_id` 与 `dataset_kind` 先做来源隔离。V2 默认仍是 BM25，显式选择 `dense` 或 `hybrid` 时使用版本化 live chunk 索引：BM25 与 Dense 各自排序，经 RRF 融合；每个 chunk 候选都会回取完整父 Evidence，并复核当前状态与 `content_hash`。索引过期、缺失、校验和不符或 Dense 失败时，返回明确标记的 `bm25_fallback`，不会把降级结果报告为 Hybrid 成功。支持 `source_types`、`evidence_ids`、`after`/`before`、`undated_only` 过滤；live 不使用 fixture 的 Entity Memory，也不会混入其他项目的数据。

```bash
# V2 live 检索模式（默认 bm25）
python main.py v2-retrieve --project-id your-project --query "回滚流程" --mode bm25
python main.py v2-retrieve --project-id your-project --query "回滚流程" --mode dense
python main.py v2-retrieve --project-id your-project --query "回滚流程" --mode hybrid

# ReAct 使用 live Hybrid 时，通过环境变量选择模式
# PowerShell: $env:LIVE_RETRIEVAL_MODE="hybrid"
python main.py --react --dataset-kind live --project-id your-project
```

**旧演示脚本评测状态**：原脚本将至少命中一条相关文档的查询比例误标为 Recall@5，现已更名为 Hit@5。旧脚本相关性仍由关键词启发式生成，历史 73.3% / 80.0% 缺少独立标注和可复现实验依据，不作为已验证结果。详见 [代码审计](docs/CODE_AUDIT.md)。

第五轮新增 [独立 Benchmark](benchmark/README.md)：历史版本冻结 27 条合成证据、14 道测试题（七类各 2）及 2 道开发题。当前任务级评测已扩展为 **16 个任务、8 个类别**，定义与评分规则见 [任务级 Benchmark](benchmark/tasks/README.md)；两者是不同代际，历史报告不覆盖、不重标。

当前评测事实（截至本版）：

- **离线回归：390 项测试通过**（含 Runtime/Session/SSE、Context、ReAct 协议、历史状态演进与 Web 工作台/Evidence 详情），不调用收费模型、不读取真实密钥。
- **任务级 Benchmark：16 任务 / 8 类别**。其中 **4 个不同任务**（`single-atlas`、`cross-atlas`、`conflict-atlas`、`decision-atlas`）做过真实模型在线运行；其余 12 个任务只有离线运行。
- **尚无 16 任务整体人工成功率**。`TaskSuccessRate` 和 `AppropriateAbstentionRate` 只在显式人工评分后才有值，当前保持 `null`；字面标记覆盖率不能替代语义成功判断。
- `decision-atlas` 第二轮在线结果的严格复评事实覆盖为 **1/2**。另一个事实依赖的标记出现在“未确认部分”标题中，严格判定不认可该标题作为事实证据，因此未放宽规则换取 2/2。
- 在线结果还包含协议失败和一次预算耗尽，不能据此声称 ReAct 已整体优于 Workflow。
- 独立 V2 合成开发集上已实测 BM25 / Dense / Hybrid；无匹配查询的空结果识别率在三种模式下均为 **1.0**，但正常查询存在 **9.09%**（1/11）的单一预期证据误拒，详见下文。
- **Benchmark V4 首次真实单任务运行（`v4-single-source-01`）未形成 Citation 交付**：5 次模型调用、4 次工具调用、30,735 provider tokens，`EvidenceRecall@K=1.0`，但 `citation_count=0`、`FactAccuracy=0`、终止原因 `budget`。这些指标只覆盖已执行的有效调用，不能推导为 `TaskSuccessRate`；后者仍为 `null`，等待独立人工评分。

详见 [评测报告](docs/BENCHMARK_EVALUATION.md) 与 [任务级评测说明](benchmark/tasks/README.md)。

首次 DeepSeek Flash 冒烟日志已完成[定向协议诊断与修复](docs/DEEPSEEK_PROTOCOL_FIX.md)：修正 ReAct 每轮硬编码 512 输出限制，保留停止原因并分类反馈。后续在线运行已改用修复后的配置，历史报告原样保留。

后续新增[调查完整性与上下文改进](docs/INVESTIGATION_CONTEXT_IMPLEMENTATION.md)：live 证据按查询选择可回取片段，保留父 ID/hash；日期过滤空结果可有界补查时间未知资料；请求历史去重并设独立字节预算。multi_round 按事实、来源和时间关联验收。**当前 390 项离线测试通过**；在线覆盖仍仅限上述任务与 Benchmark V4 的单任务首次运行。

---

## V2 增量同步与 Live Hybrid 检索

V2 保留原有 Evidence ID、`content_hash`、Evidence Ledger、Claim 校验、Decision Memory 与 ReAct 协议，并新增以下派生链路：

```
Git / Directory / CSV / JSON / JSONL
  → SourceRegistry → SyncService → EvidenceStore（current + 历史 + tombstone）
  → ChunkBuilder → 版本化 LiveIndex（BM25 + Dense + RRF）
  → LiveRetriever → ScopedSkill → ToolExecutor → EvidenceLedger → AnswerAgent
```

- **原始 Evidence 仍是唯一事实源**：chunk 只是派生检索单元，命中后必须回取完整父 Evidence；删除产生 tombstone，不物理删除历史。
- **增量与幂等**：Git 使用提交游标，目录/CSV/JSON 使用稳定记录 ID 与内容哈希；不根据 mtime 或行号推断删除，扫描不完整时不传播删除。
- **索引是缓存**：索引绑定 `project_id`、`dataset_kind`、Evidence revision、embedder 与 chunker 版本；发布前复核 revision，目录原子切换，失败不破坏当前索引。
- **不引入外部中间件**：仅使用 SQLite、本地 FAISS 文件和现有 Python 依赖。

```bash
# 1) 注册来源（CLI 输出均为 JSON）
python main.py v2-source-add --project-id your-project --connector directory \
  --source-uri "file:///path/to/docs" --config-json '{"root_path":"/path/to/docs"}'
python main.py v2-source-add --project-id your-project --connector csv \
  --source-uri "file:///path/to/issues.csv" \
  --config-json '{"path":"/path/to/issues.csv","id_column":"id","content_column":"description","category":"issue","time_column":"updated_at"}'
python main.py v2-source-add --project-id your-project --connector json \
  --source-uri "file:///path/to/events.json" \
  --config-json '{"path":"/path/to/events.json","record_path":"records","id_field":"id","content_fields":["title","body"],"time_field":"event_time","category":"document"}'

# 2) 首次全量/后续增量同步；--dry-run 只计算不写库
python main.py v2-source-list --project-id your-project
python main.py v2-sync <source_id>
python main.py v2-sync <source_id> --dry-run

# 3) 构建、校验、回滚索引
python main.py v2-index-build --project-id your-project
python main.py v2-index-status --project-id your-project
python main.py v2-index-verify --project-id your-project
python main.py v2-index-activate <index_version>

# 4) 独立 V2 合成开发集（离线缓存模型，不调用收费 LLM）
$env:HF_HUB_OFFLINE="1"; $env:TRANSFORMERS_OFFLINE="1"
python -m benchmark.v2.run_benchmark
```

报告写入 `benchmark/v2/reports/`，不修改冻结的 Benchmark V1、Gold、任务标签或历史报告。最近一次本机实测（15 个查询：11 个有标准答案，4 个负向查询；`top_k=10`；启用相关性门控）：

| 模式 | Recall@5 | Recall@10 | MRR | nDCG@10 | p50 / p95 ms | 空结果识别率 | 正常查询误拒率 | 实际模式 |
|------|----------|-----------|-----|---------|--------------|--------------|----------------|----------|
| BM25 | 0.955 | 0.955 | 0.939 | 0.919 | 9.3 / 9.6 | 1.0 | 9.09% | bm25 |
| Dense | 0.955 | 0.955 | 0.955 | 0.931 | 20.4 / 32.5 | 1.0 | 9.09% | dense |
| Hybrid (RRF) | 0.955 | 0.955 | 0.939 | 0.919 | 21.3 / 32.8 | 1.0 | 9.09% | hybrid |

同一报告中的未门控消融（`ablation_ungated`）显示：三种模式的 Recall@5/10 均为 1.0，但 Dense/Hybrid 的空结果识别率为 **0.0**（4 个负向查询全部返回最近邻），BM25 为 **0.5**；门控把空结果识别率提升到 1.0，代价是 11 个正常查询中有 1 个丢失了 1 条预期证据（`cross_source` 的 `architecture.md`，原因见下方限制）。

**最终验收与候选召回分离**：Dense/BM25/RRF 只负责召回候选；父 Evidence 是否可返回由 `src/index/relevance.py` 的独立门控决定。门控信号为：

- 查询中的精确标识符（Issue/commit/文件名/中文选项字母）在记录文本中以完整 token 出现；
- 或 meaningful-token 词法覆盖率 ≥ 校准阈值；
- 或 Dense cosine ≥ 校准阈值。

两个数值阈值分别校准，不做跨通道分数比较，也不使用 RRF 分数。策略文件为 `src/index/relevance_policy.json`，可用 `LIVE_RELEVANCE_POLICY` 指向另一份 JSON；关闭门控仅用于消融。拒绝原因、策略版本、阈值、实际检索模式和 fallback 状态都会写入 `RetrievalHits.diagnostics`、工具观察与 `TraceStep`，空结果会明确标记为 `no_candidates` 或 `all_candidates_filtered`。

```bash
# 在独立开发语料上重新校准（不使用 V2 测试集；生成新报告，不覆盖旧报告）
$env:HF_HUB_OFFLINE="1"; $env:TRANSFORMERS_OFFLINE="1"
python -m benchmark.dev.calibrate_relevance
```

当前校准结果：12 个负向开发查询在三种模式下的最大 Dense 分数为 0.741、最大词法覆盖率为 0.50；分别加 0.04 / 0.05 安全边际，得到 `dense_min_score=0.782`、`lexical_min_coverage=0.55`。开发集上正常查询误拒 0、负向查询误接收 0。

同一报告中：索引构建约 8.3 秒、17 个 chunk、约 53 KB；删除记录误召回率在重建前后均为 0；跨项目污染率为 0；索引被移走时明确返回 `bm25_fallback / index_manifest_missing / unusable`；更新记录保持同一 Evidence ID 并保留 2 个版本；已删除记录仍可按历史 ID/hash 回取。

**明确限制**：本集 Hybrid 的 Recall@5 与 BM25 持平、MRR/nDCG 未超过 Dense，**不能外推为 Hybrid 普遍更优**。词法覆盖率对“只含通用任务词 + 少量内容词”的跨来源查询偏保守：`cross_source` 的 `architecture.md` 只命中 `tombstone`，`lifecycle` 缺失，因此被过滤；这是本集唯一的正常证据误拒。Dense-only 的低重叠语义改写若 cosine 低于阈值也会被保守拒绝（宁可空结果，不返回最近邻伪证据）。这些阈值只在 9 文档开发集上校准，迁移到更大或不同领域语料前应重新校准。

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
> - **`.env` 不会被 Python import 自动读取**。`uvicorn api:app` 只导入模块，不加载 `.env`；需要环境变量时使用 `uvicorn api:app --env-file .env`，或使用 `python api.py`（该入口显式调用 `load_env_file()`）。`main.py` 也只在 CLI 入口显式加载 dotenv。

### 运行

```bash
# 默认：fixture LangGraph Workflow 演示（不是 live 自主调查）
python main.py

# ReAct 模式；不加 live 参数时仍是 fixture
python main.py --react

# Linear 对照
python main.py --linear

# 启动 API 服务（兼容 /chat 默认 fixture LangGraph；Web 工作台使用持久化 /sessions、/runs）
uvicorn api:app --reload --port 8000

# 需要从 .env 读取模型/数据库配置时，显式传 --env-file
uvicorn api:app --reload --port 8000 --env-file .env

# 等价的自带入口：先显式加载 .env，再启动 uvicorn
python api.py

# 接入真实 Git 数据
python -m src.ingest --git /path/to/your/repo --project-id your-project --dry-run
# 检查后去掉 --dry-run 执行追加导入；查询须显式选择 live 与项目
python main.py --react --dataset-kind live --project-id your-project

# 运行启发式评估（不是独立 Benchmark）
python -m src.evaluate
```

### 离线验证（不调用模型、不读取密钥）

```bash
# 390 项离线回归测试（含 Phase 2 Runtime/Session/SSE、Phase 3 Context Engineering 与 Web 工作台）
python -m unittest discover -s tests -v

# Demo 1：校验合成 CSV 导入，不写入数据库
python examples/team_collaboration/seed_demo.py --dry-run

# Demo 2：Decision Memory 演示（默认使用新建临时目录，不写入 data/live/）
python examples/team_collaboration/decision_demo.py
```

Windows 下通过管道向交互式 CLI 输入中文时，建议先设置 `PYTHONUTF8=1`（PowerShell：`$env:PYTHONUTF8="1"`），否则 Python 可能按本地代码页解码失败。该问题属于终端输入编码，不是 Agent 运行错误。

### 研发调查 Web 工作台

`web/` 是由同一 FastAPI 进程托管的轻量 HTML/CSS/JavaScript 工作台，无 Node 构建链、无 CDN 依赖。启动后访问：

```text
http://localhost:8000/demo/
```

工作台调用真实后端，不内嵌演示答案：

- 左侧创建/加载/关闭 Session，按 `project_id` + `dataset_kind` 绑定 Scope；显示当前 Session 的 Run 历史。
- 中间提交 Run，显示用户问题、最终 Agent 回答、Run 状态与终止原因。`insufficient` 明确显示为“证据不足”，不会包装成成功答案。
- 右侧三个页签：Evidence（ID、来源、locator、hash、只读内容与当前/历史版本）、Tool Calls（顺序、耗时、错误/重试、检索模式与 fallback）、Trace（生命周期事件、Context Trace、SSE 时间线）。
- 提交后立即显示 `run_id`，通过 `GET /runs/{run_id}/stream` 接收 SSE；断线后用 `after_seq`/事件 API 回填并按 `event_seq` 去重；SSE 不可用时轮询 Run 状态。
- 刷新或关闭页面只断开浏览器连接，**不会取消后台 Run**。刷新后通过 URL/localStorage 中的 `session_id`、`run_id` 恢复。
- 浏览器不保存 API Key；不会展示 Authorization Header、环境变量或数据库堆栈。页面只做只读展示，不修改 Evidence 生命周期。

Evidence 正文通过新增的只读 `GET /evidence/{evidence_id}` 获取。该端点要求显式 `project_id` + `dataset_kind`，可带 `content_hash` 固定本次 Run 实际看到的版本；跨 Scope 或 hash 不匹配返回 404，正文最多返回 12,000 字符并标记截断，不会用当前版本替换历史引用。

**离线/无凭据模式**：如果未配置模型 Key，ReAct 记录 `model_skipped` 并走离线回退。Run 仍会完成调查链并可能返回 `insufficient`；这是预期行为，不代表真实模型检索或回答质量。

**常见错误**：

| 现象 | 处理 |
|------|------|
| `/demo/` 404 | 确认从仓库根目录启动，且 `web/index.html` 存在；`create_app()` 仅在 `web/` 存在时挂载静态目录。 |
| 页面能打开但 API 503/504 | 检查 Uvicorn 进程、`EVIDENCE_DB_PATH` 与索引/数据是否就绪；页面会显示后端状态。 |
| 提交 Run 返回 409 `session_closed` | 当前 Session 已关闭；创建新 Session 或加载未关闭的 Session。 |
| 提交 Run 返回 409 `scope_mismatch` | 请求 `project_id`/`dataset_kind` 与 Session 不一致；页面使用 Session 绑定的 Scope。 |
| Evidence 详情 404 | Evidence 不属于当前 project/dataset，或 `content_hash` 不存在；页面会保留引用元数据并明确提示。 |
| SSE 断开 | 页面自动重连并按 `after_seq` 回填；多次失败后轮询 Run 状态，后台 Run 不受影响。 |
| `.env` 中配置未生效 | `uvicorn api:app` 不自动读取 `.env`；改用 `--env-file .env` 或 `python api.py`。 |

浏览器级验收脚本（需要本机 Chrome/Edge 与 `websockets`，会启动临时 Uvicorn、临时 SQLite 和合成语料；不调用收费模型）：

```bash
python tests/browser_web_demo_check.py
```

该脚本不是普通 unittest 的一部分；普通离线回归仍使用 `python -m unittest discover -s tests -v`。

完整的演示路径、数据接入、问题示例、Evidence/Trace 讲解口径与真实模型成功率边界见 [DEMO_GUIDE.md](DEMO_GUIDE.md)。

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

### 兼容问答接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/chat` | 请求级问答（JSON）；请求体含 `question`、`backend`、`scope` |
| `POST` | `/chat/stream` | 请求级问答（SSE 流式） |
| `GET` | `/health` | 健康检查 |

`/chat` 默认 `backend=langgraph` 且 scope 为 fixture；live ReAct 必须显式传 `backend=react` 与 live scope。它与持久化 Session/Run 是两套入口，不能假设二者共享对话状态。

### 持久化 Session / Run（Web 工作台所用）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/sessions` | 创建 Session；body：`project_id`、`dataset_kind` |
| `GET` | `/sessions/{session_id}` | Session、消息与最近 Run |
| `POST` | `/sessions/{session_id}/close` | 关闭 Session；之后提交 Run 返回 409 `session_closed` |
| `GET` | `/sessions/{session_id}/runs` | 当前 Session 的 Run 列表 |
| `POST` | `/runs` | 提交 Run；body：`session_id`、`question`，可带 `project_id`、`dataset_kind`、`timeout_seconds`、`budget`、`idempotency_key`（或 `Idempotency-Key` 请求头） |
| `GET` | `/runs/{run_id}?project_id=...` | Run 状态、终止原因、检索诊断字段 |
| `GET` | `/runs/{run_id}/events?project_id=...` | 持久化事件（支持 `after_seq`） |
| `GET` | `/runs/{run_id}/stream?project_id=...` | SSE；支持 `after_seq` 与 `Last-Event-ID`，断开不取消 Run |
| `GET` | `/runs/{run_id}/result?project_id=...` | 终态结果；非终态返回 409 `run_not_terminal` |
| `GET` | `/runs/{run_id}/tool-calls?project_id=...` | Tool Calls（顺序、状态、耗时、错误） |
| `GET` | `/runs/{run_id}/evidence-refs?project_id=...` | 本次 Run 实际引用的 Evidence ID/hash |
| `GET` | `/runs/{run_id}/context-trace?project_id=...` | Context Engineering 事件与预算统计 |
| `POST` | `/runs/{run_id}/cancel?project_id=...` | 协作式取消请求 |
| `GET` | `/evidence/{evidence_id}?project_id=...&dataset_kind=...` | 只读 Evidence 详情；可带 `content_hash` 固定历史版本，正文最多 12,000 字符 |
| `GET` | `/demo/` | 同源 Web 工作台；`/` 重定向到此处 |

Run 级读取必须带所属 `project_id`，跨项目按 404 处理；Evidence 详情进一步要求显式 `project_id` + `dataset_kind`，不会回退到其他项目或当前版本。SSE 事件名为 `run_created`、`run_started`、`tool_started`、`model_skipped`、`run_finished` 等，流末发送 `stream_end`。

启动后访问 `http://localhost:8000/docs` 查看完整 Swagger 文档。当前 API 没有认证和限流，只适合本地/受控环境，不能直接暴露到公网。

---

## 项目结构

```
├── main.py                  # CLI 入口（支持 --langgraph/--react/--linear）
├── api.py                   # FastAPI 服务 + /demo 静态挂载
├── web/                     # 轻量研发调查工作台（原生 HTML/CSS/JS）
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── Dockerfile / docker-compose.yml / .dockerignore
├── requirements.txt / requirements.lock
├── .env.example             # 模板；真实 .env 不进入镜像
│
├── src/
│   ├── graph.py             # LangGraph StateGraph 工作流定义
│   ├── retrieval.py         # fixture 混合检索；live BM25 / Dense / Hybrid 入口
│   ├── ingest.py            # Git/目录/结构化 CSV 导入
│   ├── evidence.py          # Evidence 身份、scope、时间、hash
│   ├── evidence_store.py    # append-only SQLite 证据存储 + current/tombstone/索引元数据
│   ├── chunking.py          # 既有 chunker + 稳定 char/line locator
│   ├── sync/                # V2 source registry、connectors、事务写入、同步服务
│   │   ├── connectors.py    # Git / Directory / CSV / JSON / JSONL（不执行外部代码）
│   │   ├── service.py       # SourceRegistry、SyncService、cursor、dry-run
│   │   ├── writer.py        # 按批次事务写入、current 状态、tombstone
│   │   └── models.py        # SourceSpec / ScanBatch / SyncReport
│   ├── index/               # V2 派生 chunk 与版本化 FAISS 索引
│   │   ├── chunks.py        # 稳定 chunk ID、父 Evidence 关联、locator
│   │   ├── builder.py       # 构建、manifest、校验、原子发布、恢复
│   │   ├── store.py         # index version / alias / 成员元数据
│   │   └── retriever.py     # BM25/Dense/RRF、父 Evidence 回取、显式 fallback
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
├── benchmark/v2/             # 独立 V2 合成开发集、指标与 live 检索报告
├── benchmark/v3/             # 独立 Phase 3 上下文构造评测（不覆盖 V1/V2）
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
- **Hybrid RAG（fixture）**：Dense + Sparse + Entity 三路检索，RRF 融合；V2 live 支持 BM25/Dense/Hybrid 与显式 fallback
- **ReAct Agent**：自适应 Think-Act-Observe 循环，动态选择工具和查询策略；可显式切到 live
- **LLM 路由 + 关键词 Fallback**：既利用 LLM 语义理解，又保证离线可用
- **长期记忆分层**：Decision/Episodic/Semantic/Entity/Relationship 结构；其中 Decision 有版本/审核 CLI，其余仍以静态数据为主
- **Multisource 增量同步**：Git 游标、目录/CSV/JSON 稳定身份、幂等写入、tombstone 与历史回取；删除不物理清除
- **版本化 LiveIndex**：manifest/checksum、revision 快照、原子 alias 切换、失败隔离与回滚
- **证据化输出**：回答+证据+来源+实体+置信度+路由；live ReAct 额外校验 Evidence ID、hash、scope 和原文摘录
- **多轮对话记忆**：追问感知，窗口滑动 + 历史摘要
- **双模式部署**：CLI 交互 + FastAPI 流式服务 + Docker 容器化
- **离线 Benchmark/回归**：冻结 V1 合成集保持不变；V2 独立开发集报告与 390 项离线测试通过。真实模型仅覆盖 4 个 V1 任务与 Benchmark V4 单任务首次运行，尚无完整真实模型语义验收。

## Docker 部署与安全边界

- 构建上下文由 `.dockerignore` 排除 `.env*`（保留 `.env.example`）、私钥、`venv/`、缓存、`data/live/`、SQLite/FAISS 索引、`benchmark/reports/`、测试和临时报告。
- Dockerfile 在 `COPY . .` 后执行 `test ! -e .env ...`，敏感文件意外进入上下文时构建失败。
- 容器以非 root 的 `10001:10001` 运行；compose 额外启用 `no-new-privileges` 并丢弃全部 capabilities。
- `data/` 通过运行时 volume 挂载，不烘焙进镜像；`db_md` 使用命名 volume 保存构建期 fixture 索引。若要在容器内写入 `data/live/`，需让宿主机目录对 UID 10001 可写；更简单的做法是先在宿主机运行 seed 脚本，再启动容器。
- 构建期 fixture 索引失败不会再静默：会打印警告并继续，live 检索不依赖该索引。
- **未验证项**：当前环境没有 Docker CLI，未实际执行 `docker build`/`docker compose config`；Dockerfile/compose 中的 Python 3.11 依赖安装与容器内运行均未验证。上述仅为静态配置修改。

## 未实现或未验证的能力

以下内容**不能**表述为已完成：

- **外部 SaaS 接入**：没有实现飞书、Slack、Jira、Confluence 等外部 SaaS 的在线导入/同步；当前入口是 Git、目录、结构化 CSV/JSON/JSONL。
- **自动调度与 Webhook**：V2 同步是显式 CLI 调用，没有后台调度器、Webhook 或跨进程同步队列；并发同步使用 SQLite 运行标记与 cursor 乐观版本控制，不提供分布式锁。
- **V2 真实模型语义验收**：本轮只完成离线脚本化 ReAct 验收和检索指标；尚未用真实 DeepSeek 对 V2 合成集做语义正确性/引用正确性人工评分。
- **Connector 边界**：目录扫描遇到不可读或非 UTF-8 文件会中止整次扫描（因此不会误删），但不会跳过并继续；CSV 的 `schema_hash` 仍是轻量列签名，不是完整 schema 演进系统；不会执行 CSV/JSON 中的任何代码或 Shell。
- **Live 索引增量性**：当前采用整库重建（可接受），没有 FAISS 向量级增量删除；旧版本索引保留用于回滚，但尚未实现自动过期清理策略。
- **Chunk locator 粒度**：结构化/代码 chunk 可提供精确 offset 与行号；部分 Markdown chunker 会规范化空行，这类 chunk 标记为 `unavailable`，只能通过父 Evidence + 查询片段生成摘录，不能直接作为已验证 Excerpt。
- **空结果策略的边界**：Dense/Hybrid 的最近邻候选现在会经过校准门控，无匹配查询在 V2 测试集上识别率为 1.0；但门控是保守的词法/绝对 cosine 规则，不是重排模型。低重叠语义改写可能被误拒，阈值也只在 9 文档开发集上校准。门控不满足时应返回空结果并在 diagnostics/Agent gap 中披露，而不是返回最近邻伪证据；更换领域或 embedder 后必须重新校准。
- **Docker 实际构建**：本机无 Docker CLI，镜像构建与容器运行未验证。
- **16 任务整体在线成功率**：只有 4 个任务有真实模型运行记录，且没有对全部任务做人工评分。
- **Benchmark V4 真实模型调查成功率**：仅执行过 `v4-single-source-01` 一次真实运行，未形成最终可引用答案；真实模型多轮调查的成功率尚未得到充分验证。`EvidenceRecall@K=1.0` 只表示本次已交付 Evidence 覆盖了 Gold 组，不等于 `TaskSuccessRate=100%`，也不等于 Citation 的语义支持已自动验证。
- **持久化运行时范围**：`/sessions`、`/runs` 已提供跨请求持久化 Session/Run、事件重放和追问上下文；兼容 `/chat`、`/chat/stream` 仍是请求独立实例。没有多租户认证、全局 Session 列表、后台调度或跨进程分布式运行时。

## 工程审计与后续开发

当前默认模式为固定 LangGraph Workflow，可选 ReAct 执行动态调查。五类长期记忆主要为静态 JSON；Decision Memory 有不可变版本、提案/审核和冲突记录，写入/审核仍是 CLI。只读查询已支持标题+版本正文、`current/history`、`known_at` 和业务时间范围；任务覆盖是确定性的目标/字面标记存在性检查，明确不声称语义完整性。SSE 提供节点进度、最终答案和结构化错误事件；任一后端异常后都会发送终止标记 `data: [DONE]`。

- [代码审计与修复验证](docs/CODE_AUDIT.md)
- [Agent 主导增量路线](docs/AGENT_ROADMAP.md)
- [真实证据接入与 Evidence 实现](docs/EVIDENCE_IMPLEMENTATION.md)
- [ReAct 调查、引用与执行轨迹](docs/REACT_INVESTIGATION_IMPLEMENTATION.md)
- [Decision Memory 审核与 LLM 冒烟准备](docs/DECISION_MEMORY_IMPLEMENTATION.md)
- [Phase 3 上下文预算、证据选择、压缩与可观测性](docs/AGENT_RUNTIME_PHASE3.md)
- 结构化接入说明：[examples/team_collaboration/README.md](examples/team_collaboration/README.md)
- 离线回归：`python -m unittest discover -s tests -v`，测试依赖见 `requirements-dev.txt`。新增结构化导入测试不调用模型、不读取密钥。
