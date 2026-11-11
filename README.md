# Collaborative Memory Agent

**证据型只读研发历史调查 Agent**

把分散在聊天、会议、需求与评审文档、Issue、Git Commit、日志和决策记录中的研发历史，统一接入为可追溯的 **Evidence**；由 ReAct Agent 自主检索、核对证据身份与原文，交付带引用、可复核的调查结论，或在证据不足时明确拒答。

> **只读边界**：调查运行时只读取 Evidence 与索引，不修改原始记录；数据写入只发生在显式的导入/同步 CLI。
>
> **两条运行路径**：未配置 API Key 时可离线运行，用于验证检索、Runtime、SSE、Evidence 与 Trace；真实模型调查需要自备 DeepSeek API Key（收费）。

---

## 目录

- [核心能力](#核心能力)
- [系统架构](#系统架构)
- [关键机制](#关键机制)
- [快速开始（Windows PowerShell）](#快速开始windows-powershell)
- [评测状态](#评测状态)
- [局限与未实现能力](#局限与未实现能力)
- [项目结构](#项目结构)
- [文档索引](#文档索引)
- [许可证](#许可证)

---

## 核心能力

| 能力 | 实现要点 | 入口 |
|---|---|---|
| **多源 Evidence 同步与增量更新** | Git 提交游标、目录/CSV/JSON/JSONL 稳定记录 ID + `content_hash`、幂等写入、删除产生 tombstone、扫描不完整时不传播删除 | `python main.py v2-source-add` / `v2-sync` |
| **BM25 / Dense / Hybrid 检索** | 版本化 live chunk 索引；BM25 与 Dense 各自排序后 RRF 融合；命中 chunk 必须回取完整父 Evidence；索引缺失/过期/校验失败时返回明确标记的 `bm25_fallback` | `v2-index-*` / `v2-retrieve`；`LIVE_RETRIEVAL_MODE=hybrid` |
| **ReAct 自主工具调查** | Think-Act-Observe 循环；默认最多 **5 个模型决策轮**，工具调用单独计数（默认上限 8 次）；模型调用、Token、工具调用与 deadline 均有预算 | `python main.py --react` |
| **EvidenceLedger 引用校验** | 校验 Evidence ID、`content_hash`、Scope、本次运行来源、当前 active 状态；Claim 文本必须是引用原文的逐字子串；片段（excerpt）按字符偏移复核 | `src/investigation.py` / `src/investigation_context.py` |
| **Session / Run 持久化、SSE、Trace** | SQLite 事件流；提问幂等、协作式取消、超时、重启中断标记、断线重放、Context Trace 与 Run 查询 | `/sessions`、`/runs`、`/runs/{id}/stream` |
| **Web 调查工作台** | 同源只读工作台：Evidence、Tool Calls、Trace 三个页签；浏览器不保存 Key，不展示 Authorization Header | `http://localhost:8000/demo/` |

---

## 系统架构

### 1. 数据接入与索引链路

```text
Git / Directory / CSV / JSON / JSONL
        │
        ▼
SourceRegistry（来源注册）
        │
        ▼
SyncService（游标 / 稳定 ID / 内容哈希 / 幂等 / dry-run）
        │
        ▼
EvidenceStore（append-only；current + 历史版本 + tombstone）
        │
        ▼
ChunkBuilder（稳定 chunk ID、父 Evidence 关联、locator）
        │
        ▼
版本化 LiveIndex（BM25 + Dense；manifest / checksum / 原子切换 / 回滚）
```

- 原始 Evidence 是唯一事实源；chunk 与索引只是派生检索单元。
- 索引绑定 `project_id`、`dataset_kind`、Evidence revision、embedder 与 chunker 版本；发布前复核 revision。
- 删除产生 tombstone，不物理删除历史；历史 ID/hash 仍可回取。

### 2. 在线调查与引用交付链路

```text
问题 + Scope（project_id, dataset_kind）
        │
        ▼
Session / Run Runtime（持久化事件、幂等、取消、超时）
        │
        ▼
ReAct 决策轮（最多 5 轮；工具调用另计，默认上限 8）
        │
        ├── 检索工具：BM25 / Dense / Hybrid（显式 fallback）
        ├── 读取工具：按 Evidence ID + content_hash 取父 Evidence / 片段
        └── finish：提交 Claim + EvidenceRef + gaps / conflicts
        │
        ▼
EvidenceLedger 校验（身份 / hash / Scope / 本次运行 / active / 逐字摘录）
        │
        ▼
答案交付：带引用结论，或 insufficient / conflict / budget
        │
        ▼
SSE 事件 · Tool Calls · Context Trace · Run 结果持久化
```

终止原因不是成功标志：`insufficient`、`conflict`、`budget`、`model_error` 都表示本次调查未形成可确认结论。

### 3. 旧模式对照（非主链路）

以下模式保留用于对照和回归，不是 live 自主调查入口：

- **LangGraph Workflow**：`python main.py`，默认 fixture 数据，并行 Skill fan-out/fan-in。
- **Linear**：`python main.py --linear`，原始顺序流水线，用于 A/B 对比。
- **ReAct fixture**：`python main.py --react`，未加 live 参数时仍使用 fixture 数据。

正式的 live ReAct 调查必须显式指定 `--dataset-kind live --project-id ...`，并先导入该项目的数据。

---

## 关键机制

### Evidence 身份与引用校验

一条 Evidence 由 `evidence_id`、`content_hash`、`project_id`、`dataset_kind`、来源 locator、事件时间和版本状态共同约束。调查过程中的每条 Claim 引用都要经过 EvidenceLedger：

1. Evidence 必须存在于本次运行的可见范围；
2. `project_id` / `dataset_kind` 必须与当前 Scope 一致；
3. `content_hash` 必须与本次读取的版本一致；
4. Evidence 必须是当前 active 状态；
5. Claim 文本必须是所引原文的逐字子串；
6. 片段引用必须通过字符偏移与父 Evidence 原文复核。

这能证明**引用身份与逐字摘录合法**，但不能自动证明结论在语义上一定由引用支持。语义支持仍需独立人工复核。

### 检索与隔离

- live 检索按 `project_id` + `dataset_kind` 先做来源隔离，不混入其他项目数据。
- 默认模式为 BM25；可显式选择 `dense` 或 `hybrid`。
- Dense/Hybrid 的候选经过相关性门控后才可返回；门控不满足时返回空结果并披露原因，不返回最近邻伪证据。
- 索引缺失、过期、校验和不符或 Dense 失败时，结果标记为 `bm25_fallback`，不会报告为 Hybrid 成功。
- 门控阈值是保守的词法/绝对 cosine 规则，只在小型开发集上校准；更换领域、语料或 embedder 后必须重新校准。当前策略见 `src/index/relevance_policy.json`。

### Runtime 与可观测性

- Session 绑定 Scope；Run 必须使用同一 `project_id` + `dataset_kind`。
- 事件按 `event_seq` 持久化，SSE 支持 `after_seq` / `Last-Event-ID` 断线重放；断开浏览器连接不会取消后台 Run。
- Tool Calls 记录顺序、状态、耗时、检索模式、fallback 与过滤诊断。
- Context Trace 记录输入/输出预算、估算 token、证据选择与压缩原因。
- 当前 API **没有认证与限流**，只适合本地或受控环境，不能直接暴露到公网。

---

## 快速开始（Windows PowerShell）

### 0. 环境

- Python 3.12.7（当前已验证环境）；Docker 使用 Python 3.11，容器内安装尚未验证。
- 不需要 Node 构建链；Web 工作台是原生 HTML/CSS/JS。

### A. 离线验证（不读取 Key、不调用收费模型）

```powershell
python -m pip install -r requirements.txt

# 393 项离线回归测试
python -m unittest discover -s tests -v

# 默认 fixture LangGraph Workflow 演示（不是 live 自主调查）
python main.py
```

> **Embedding 模型首次下载**：以下路径会加载 `sentence-transformers` 嵌入模型：Dense/Hybrid 检索（`v2-retrieve --mode dense|hybrid`）、构建 live 索引，以及默认 fixture Workflow 中 MemorySkill 的 hybrid 检索——本地尚无 `data/vector_store/` 索引时会现场编码文档并建索引。模型为 `BAAI/bge-small-zh-v1.5`，首次使用会从 Hugging Face 下载（需要网络，之后使用本地缓存；索引与模型缓存均已被 `.gitignore` 排除）。纯 BM25 模式（如 `v2-retrieve --mode bm25`）和显式 mock/禁用 hybrid 检索的测试不会触发下载；但直接实例化 `MemorySkill` 的离线测试在没有本地索引时同样会尝试构建 Dense 索引。
>
> 嵌入模型下载不等于调用收费 LLM；它不读取 API Key，也不产生 LLM API 费用。

### B. 真实模型模式（收费 API）

```powershell
Copy-Item .env.example .env
# 编辑 .env，只填写真实的 DEEPSEEK_API_KEY
# DEEPSEEK_BASE_URL 与 DEEPSEEK_MODEL 留空时默认使用官方 endpoint 与 deepseek-flash

# 入口 A：显式加载 .env 后启动（推荐）
python api.py

# 入口 B：uvicorn 显式传入 env 文件
uvicorn api:app --port 8000 --env-file .env
```

> `uvicorn api:app` 单独使用时不会自动读取 `.env`。真实模型调用会产生费用；API Key 只保存在本地 `.env`，不会写入浏览器、Run 结果或 Git。

### C. 接入合成演示数据并打开 Web 工作台

```powershell
# 1) 先 dry-run 校验 CSV 映射与记录数
python examples/team_collaboration/seed_demo.py --dry-run

# 2) 写入独立 live SQLite（默认项目 cma-team-demo）
python examples/team_collaboration/seed_demo.py `
  --project-id cma-team-demo `
  --db data/live/team_demo.sqlite3

# 3) 让 API 使用同一数据库并启动
$env:EVIDENCE_DB_PATH = "data/live/team_demo.sqlite3"
python api.py
```

浏览器访问 `http://localhost:8000/demo/`。演示数据全部为合成团队协作记录；`data/live/` 已被 `.gitignore` 排除。

### D. 可选：演示 Dense / Hybrid

```powershell
python main.py v2-index-build --project-id cma-team-demo
python main.py v2-index-verify --project-id cma-team-demo
$env:LIVE_RETRIEVAL_MODE = "hybrid"
python main.py --react --dataset-kind live --project-id cma-team-demo --db data/live/team_demo.sqlite3
```

### E. 常用 CLI 摘要

| 命令 | 用途 |
|---|---|
| `python main.py` | 默认 fixture LangGraph Workflow 演示 |
| `python main.py --react` | ReAct（fixture，除非显式加 live） |
| `python main.py --linear` | Linear 对照 |
| `python main.py --react --dataset-kind live --project-id <id> --db <path>` | 正式 live ReAct 调查 |
| `python -m src.ingest --git <repo> --project-id <id> --dry-run` | Git 数据导入（先 dry-run） |
| `python main.py v2-source-add` / `v2-sync` | 注册来源、增量同步 |
| `python main.py v2-index-build` / `v2-index-verify` / `v2-index-activate` | 构建、校验、启用版本化索引 |
| `python main.py v2-retrieve --project-id <id> --query <q> --mode bm25` | 直接检索（bm25/dense/hybrid） |
| `python api.py` | FastAPI + Web 工作台 |

完整的演示路径、CSV 字段与故障排查见 [DEMO_GUIDE.md](DEMO_GUIDE.md)；各子命令参数可用 `python main.py <subcommand> --help` 查看。

---

## 评测状态

### 离线回归

**393 项离线测试通过**，覆盖 Runtime/Session/SSE、Context Engineering、ReAct 协议、证据与检索、历史状态演进和 Web 工作台。离线测试不调用收费模型、不读取真实密钥；个别直接走 MemorySkill 检索的测试在无本地索引时可能触发一次 embedding 模型加载，但不产生 LLM API 调用。

### Benchmark V4 数据集

| 项目 | 值 |
|---|---|
| 快照 | `cma-v4-658dd583ab18a47f` / `cma-v4-tasks-v1` / `cma-v4-scoring-v1` |
| 原始观察 | 414 条 |
| Evidence 身份 | 396 个 |
| 合成项目 | 3 个 |
| 任务 | 45 个 |
| 类别 | 9 类 × 5（single_source、cross_source、multi_step、decision_history、conflict、insufficient、budget、tool_failure、multi_turn） |
| 难度 | easy 5 / medium 25 / hard 15 |
| 标注状态 | `synthetic_authored_pending_independent_human_review` |

数据与 Gold 为确定性合成产物；Gold 尚未完成独立人工审核与互评者一致性测量。详见 [benchmark/v4/README.md](benchmark/v4/README.md)。

### 真实模型运行记录（5 次运行，2 个任务）

| # | 日期 | 任务 | 运行结果 | 模型调用 | 工具调用 | provider tokens（输入/输出） | Citations | FactAccuracy |
|---|---|---|---|---|---|---|---|---|
| 1 | 2026-11-02 | `v4-single-source-01` | `model_error` | 1 | 0 | 1,348 / 0 | 0 | 0.0 |
| 2 | 2026-11-02 | `v4-single-source-01` | `budget`（token） | 1 | 2 | 4,752 / 285 | 0 | 0.0 |
| 3 | 2026-11-02 | `v4-single-source-01` | `conflict` | 4 | 3 | 27,561 / 2,202 | 0 | 2/3 |
| 4 | 2026-11-02 | `v4-single-source-01` | `budget`（token） | 5 | 4 | 28,517 / 2,218 | 0 | 0.0 |
| 5 | 2026-11-03 | `v4-single-source-03` | `insufficient` | 4 | 3 | 31,878 / 1,999 | 4 | 1.0 |

> **这 5 次是运行次数，不是 5 个不同任务**：其中 4 次都在 `v4-single-source-01` 上，只有第 5 次切换到 `v4-single-source-03`。总量很小，不能据此推断 45 任务的整体成功率。

#### `v4-single-source-01` 的失败记录

4 次运行均未形成 Citation 交付（`citation_count=0`），分别以 `model_error`、`budget`（token）、`conflict`、`budget`（token）结束。第 3 次运行交付了 2/3 必需事实，但未产生有效引用；第 4 次运行在一次 5 模型调用 / 4 工具调用、30,735 provider tokens 的尝试后耗尽预算。这些记录说明：检索召回、工具调用和最终引用交付是不同环节，不能用其中一个环节的指标替代整体任务成功。

#### `v4-single-source-03` 的部分成功

| 指标 | 结果 |
|---|---|
| Claims / Citations | 4 / 4（4 条唯一 Evidence 引用） |
| Gold Evidence 组 | 2 / 2 |
| 必选事实 / 可选事实 | 3 / 3 · 1 / 1 |
| `EvidenceRecall@K` | 1.0 |
| `CitationValidity` | 1.0 |
| `FactAccuracy` / `OptionalFactAccuracy` | 1.0 / 1.0 |
| `termination_reason` | `insufficient`（`finish_validated`） |
| 预期终止 | `sufficient` |
| `TaskSuccessRate` | `null`（待独立人工评分） |

四条 Claim 均通过 Evidence ID、`content_hash`、Scope、本次运行来源和 active 状态校验，且 Claim 文本是引用原文的逐字子串。但运行终止为 `insufficient`，与冻结 Gold 的预期 `sufficient` 不一致，因此**严格验收只能记为部分成功（provisional）**：核心答案与引用成功，严格终止状态未达标。

评分器修复只影响 Benchmark V4 的终止匹配：`termination_matches_expected` 现在优先读取 `Gold.expected_abstention.expected_termination`，并保留旧顶层字段的兼容路径。使用临时物化快照和原始 `raw` 做只读重评后，该字段为 `false`；`EvidenceRecall@K`、`CitationValidity`、`FactAccuracy` 仍为 1.0。**原始报告保持不变**，未重写、未重新发送模型请求。详见 [最终验收记录](benchmark/v4/acceptance/final_acceptance_v4_single_source_03.md)。

### 指标口径

| 指标 | 能证明 | 不能证明 |
|---|---|---|
| `CitationValidity` | Evidence 存在、Scope 正确、`content_hash` 一致、来自本次运行、当前 active；Claim 文本逐字出现在被引原文中 | 结论在语义上一定由引用支持 |
| `FactAccuracy` | 答案包含 Gold 的结构化事实或标记组 | 事实是由引用语义推导出来的 |
| `EvidenceRecall@K` | Gold 要求的 Evidence 组被实际交付 | 答案正确使用了这些 Evidence |
| `termination_matches_expected` | 运行终止原因与 Gold 预期一致 | 答案内容正确 |

因此，本项目**不宣称**已获得完整任务成功率；`TaskSuccessRate` 只有在具名 reviewer 完成独立人工评分后才可计算，否则保持 `null`。

### 历史评测

更早的 V1 冻结 Benchmark（16 任务 / 8 类别，其中 4 个任务有在线记录）、V2 合成开发集检索指标与相关性门控校准、V3 上下文构造评测，均保留在独立目录和文档中，不与 V4 分数混用：

- [benchmark/README.md](benchmark/README.md) — V1 冻结 Benchmark
- [benchmark/tasks/README.md](benchmark/tasks/README.md) — 16 任务 / 8 类别任务级评测
- [docs/BENCHMARK_EVALUATION.md](docs/BENCHMARK_EVALUATION.md) — 历史指标、失败案例与限制
- [benchmark/v3/README.md](benchmark/v3/README.md) — Phase 3 上下文构造评测
- [benchmark/v4/README.md](benchmark/v4/README.md) — V4 数据集、评分与运行器

---

## 局限与未实现能力

以下内容**不能**表述为已完成：

- **真实模型覆盖率有限**：只有少数任务做过真实模型运行，且没有 45 任务整体的人工成功率；`TaskSuccessRate` 仍为 `null`。
- **自动指标不是语义验收**：`FactAccuracy` 与 `CitationValidity` 是结构化/身份级校验，不替代独立语义复核。
- **外部 SaaS 接入**：未实现飞书、Slack、Jira、Confluence 等在线导入；当前入口是 Git、目录、结构化 CSV/JSON/JSONL。
- **自动调度与 Webhook**：同步是显式 CLI 调用，没有后台调度器、Webhook 或跨进程队列。
- **多租户与公网安全**：API 没有认证和限流，只适合本地/受控环境，不能直接暴露到公网。
- **Docker 未实际构建**：当前环境没有 Docker CLI，镜像构建与容器运行未验证。
- **Live 索引全量重建**：没有 FAISS 向量级增量删除；旧版本索引保留用于回滚，但没有自动过期清理。
- **长期记忆成熟度**：Decision Memory 有不可变版本、审核与冲突记录，但写入/审核仍是 CLI；其余四类记忆主要是静态 JSON。
- **相关性门控边界**：门控是保守规则，不是重排模型；低重叠语义改写可能被误拒，更换领域或 embedder 后必须重新校准。
- **fixture 默认模式的边界**：`python main.py` 默认运行 fixture LangGraph 演示，不代表 live 自主调查或真实模型质量。

---

## 项目结构

```text
├── main.py / api.py              # CLI 与 FastAPI 入口
├── web/                          # 原生 HTML/CSS/JS 调查工作台
├── src/
│   ├── sync/                     # 来源注册、连接器、增量同步、事务写入
│   ├── index/                    # chunk、版本化 LiveIndex、BM25/Dense/RRF、相关性门控
│   ├── runtime/                  # Session/Run 持久化、事件存储、SSE 支撑
│   ├── api/                      # 持久化 Session/Run 路由
│   ├── agents/                   # Coordinator / ReAct / Answer
│   ├── skills/                   # 五类检索 Skill
│   ├── investigation.py          # ReAct 协议与 EvidenceLedger
│   ├── investigation_context.py  # 证据片段、上下文预算、Excerpt 校验
│   ├── evidence.py / evidence_store.py
│   ├── retrieval.py / ingest.py / chunking.py
│   └── memory/                   # 对话记忆与 Decision Memory
├── benchmark/                    # V1/V2/V3/V4 独立评测（数据、运行器、验收记录）
├── examples/team_collaboration/  # 全合成 CSV 语料与演示脚本
├── data/                         # fixture 数据；data/live/ 为本地 live 导入（不入库）
├── docs/                         # 设计与审计文档
├── DEMO_GUIDE.md                 # 演示路径、常见问题与检查清单
├── Dockerfile / docker-compose.yml / .dockerignore
└── requirements.txt / requirements.lock
```

---

## 文档索引

- [DEMO_GUIDE.md](DEMO_GUIDE.md) — 演示路径、数据接入、Web 工作台与故障排查
- [docs/AGENT_RUNTIME_PHASE2.md](docs/AGENT_RUNTIME_PHASE2.md) — Session/Run、SSE、幂等与恢复
- [docs/AGENT_RUNTIME_PHASE3.md](docs/AGENT_RUNTIME_PHASE3.md) — 上下文预算、证据选择、压缩与 Trace
- [docs/EVIDENCE_IMPLEMENTATION.md](docs/EVIDENCE_IMPLEMENTATION.md) — Evidence 身份、存储与校验
- [docs/REACT_INVESTIGATION_IMPLEMENTATION.md](docs/REACT_INVESTIGATION_IMPLEMENTATION.md) — ReAct 协议、引用与执行轨迹
- [docs/INVESTIGATION_CONTEXT_IMPLEMENTATION.md](docs/INVESTIGATION_CONTEXT_IMPLEMENTATION.md) — 证据片段与上下文构造
- [docs/DECISION_MEMORY_IMPLEMENTATION.md](docs/DECISION_MEMORY_IMPLEMENTATION.md) — Decision Memory 版本、审核与冲突
- [docs/CODE_AUDIT.md](docs/CODE_AUDIT.md) — 代码审计与历史指标纠偏
- [examples/team_collaboration/README.md](examples/team_collaboration/README.md) — 结构化 CSV 导入格式

---

## 许可证

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。仓库内的 Benchmark 数据与演示语料均为确定性合成数据。
