# Collaborative Memory Agent V2 · 演示指南

本文面向演示操作者，给出一条不依赖外部 SaaS、默认不发送收费模型请求的演示路径。
所有展示都来自真实 FastAPI Runtime 与 SQLite Evidence Store，不内嵌答案。

## 0. 先说明边界

- 真实模型调查成功率**尚未得到充分验证**。不要用一次运行或单个指标宣称系统达到高准确率。
- `EvidenceRecall@K=1.0` 只表示本次交付的 Evidence 覆盖了 Gold 组，**不等于** `TaskSuccessRate=100%`，也不等于 Citation 的语义支持已自动验证。
- `insufficient`、`conflict`、`budget`、`model_error` 都是终止结果，不是“成功找到答案”。
- 未配置 API Key 时系统走离线回退；这能演示 Runtime、检索、证据与 Trace，但不能代表真实模型回答质量。
- 本演示不会读取 `.env` 以外的凭据，也不会把 Key 写入浏览器或 Run 结果。

## 1. 环境与启动（Windows PowerShell）

```powershell
# 在仓库根目录
python -m pip install -r requirements.txt

# 可选：需要真实模型时再配置；不配置也能完成离线演示
Copy-Item .env.example .env
# 编辑 .env，仅填写 DEEPSEEK_API_KEY 等真实值；不要把 .env 提交到 Git
```

`.env` 不会被 import 自动读取。两种正确入口：

```powershell
# 入口 A：显式加载 .env 后启动（推荐用于演示）
python api.py

# 入口 B：uvicorn 显式传入 env 文件
uvicorn api:app --port 8000 --env-file .env
```

不需要模型配置时可省略 `.env`，直接运行 `python api.py`。页面访问：

```text
http://localhost:8000/demo/
```

`GET /` 会重定向到 `/demo/`；`/health` 与 `/docs` 仍由 API 提供。

## 2. 数据接入（使用仓库自带合成语料）

演示数据全部为合成团队协作记录，覆盖需求、聊天、会议、评审、Issue、文档与部署记录。

```powershell
# 1) 先 dry-run，确认 CSV 映射与记录数
python examples/team_collaboration/seed_demo.py --dry-run

# 2) 写入独立的 live SQLite（脚本默认不会写 data/live/，这里显式指定演示库）
python examples/team_collaboration/seed_demo.py `
  --project-id cma-team-demo `
  --db data/live/team_demo.sqlite3

# 3) 让 API 使用同一数据库
$env:EVIDENCE_DB_PATH = "data/live/team_demo.sqlite3"
python api.py
```

说明：

- `data/live/team_demo.sqlite3` 是本地演示数据库，已在 `.gitignore` 中排除，不进入提交。
- 默认 live 检索为 BM25，不需要向量索引即可演示。
- 如需演示 Dense/Hybrid，先构建并校验版本化索引，再设置 `LIVE_RETRIEVAL_MODE=hybrid`：
  ```powershell
  python main.py v2-index-build --project-id cma-team-demo
  python main.py v2-index-verify --project-id cma-team-demo
  $env:LIVE_RETRIEVAL_MODE = "hybrid"
  ```
  索引文件与模型缓存均不提交。

## 3. 在 Web 工作台发起调查

1. 打开 `http://localhost:8000/demo/`，确认右上角连接状态为可用、`/health` 正常。
2. 左侧「项目范围」填写：
   - `project_id`：`cma-team-demo`
   - `dataset_kind`：`live`
3. 点击「创建 Session」。Scope 会绑定到该 Session；后续 Run 必须使用同一 `project_id` + `dataset_kind`。
4. 在中间输入问题并提交。可直接演示的问题：

   ```text
   Q2 是否迁移向量数据库？请给出会议、聊天和文档证据。
   ```

   其他可选问题：

   ```text
   研发部署方案使用什么技术？
   部署方案是否使用 SQLite？
   回滚流程在文档和评审中分别是什么状态？
   ```

5. 提交后立即显示 `run_id`，答案、状态与终止原因在中间区域更新；右侧显示 Evidence、Tool Calls 与 Trace。
6. 刷新页面不会取消后台 Run。使用 URL 或 localStorage 中的 `session_id` / `run_id` 重新加载即可恢复。

## 4. 查看工具执行

右侧「Tool Calls」页签按 ordinal 顺序展示：

- 工具名与参数（敏感字段已由后端裁剪）；
- 成功/失败/重试状态与耗时；
- 检索模式、fallback、候选数量与过滤诊断；
- 空结果会明确显示为 `no_candidates` / `all_candidates_filtered`，不会被包装成命中。

演示要点：Agent 的每一轮调查都应能在 Tool Calls 和 Trace 中回溯，而不是只展示最终一段文字。

## 5. 查看 Evidence

右侧「Evidence」页签展示后端返回的引用：

- `evidence_id`、`source_type`、`source_uri`、locator；
- `content_hash` 与当前/历史版本状态；
- 只读正文（最多 12,000 字符，截断会标记）；
- 跨 Scope 或 hash 不匹配时详情返回 404，页面保留引用元数据并提示。

Evidence 详情来自只读接口：

```text
GET /evidence/{evidence_id}?project_id=...&dataset_kind=live&content_hash=...
```

该接口不会用当前版本替换历史引用，也不会返回其他项目/数据集的内容。

## 6. 查看结果与 Trace

右侧「Trace」页签包含：

- SSE 生命周期事件（按 `event_seq` 去重，断线后按 `after_seq` 回填）；
- Context Trace（输入/输出预算、估算 token、压缩与选择原因）；
- 最终 Run 状态与 `termination_reason`。

必须如实解释：

| 状态/终止原因 | 演示口径 |
|---|---|
| `succeeded` / `sufficient` | 后端认为证据足以支持结论；页面未做独立语义复核 |
| `insufficient` | 证据不足或必要条件未满足，不是成功答案 |
| `conflict` | 存在未解决冲突，不能压成单一结论 |
| `budget` | 轮次、Token 或时限耗尽，调查未完成 |
| `model_error` | 模型调用失败（含认证/网络/协议错误） |
| `cancelled` / `timed_out` / `interrupted` | 非成功终止 |

## 7. 无 Key / 离线回退

- 未配置 Key 时，ReAct 会记录 `model_skipped` 并走离线回退；Run 仍可完成调查链，但可能返回 `insufficient`。
- 这适合展示 Session、Run、SSE、Tool Calls、Evidence、Trace 和拒绝逻辑。
- **不要**把离线回退的回答当作真实模型能力；也不要为了演示效果伪造 `succeeded`。

## 8. 建议的 5–8 分钟演示脚本

1. 说明这是只读调查工作台，Scope 由 `project_id` + `dataset_kind` 隔离。
2. 展示 dry-run 数据接入，再展示已写入的合成 live 数据库。
3. 创建 Session，提交一个问题。
4. 在 Tool Calls 中说明 Agent 检索了哪些来源、是否发生过滤/fallback。
5. 在 Evidence 中打开一条引用，核对 ID、hash、locator 与原文。
6. 在 Trace 中说明终止原因和预算；若为 `insufficient`/`budget`，明确说“本次未完成，不是成功”。
7. 总结：系统已验证的是可追溯的 Runtime/证据链；真实模型成功率仍需更大规模、独立人工评分。

## 9. 常见问题

| 现象 | 处理 |
|---|---|
| `/demo/` 404 | 从仓库根目录启动，确认 `web/index.html` 存在 |
| 页面可用但 API 报错 | 检查 Uvicorn、`EVIDENCE_DB_PATH`、演示数据库是否已 seed |
| 创建 Session 失败 | 确认 `project_id` 非空；`dataset_kind` 只能是 `live` 或 `fixture` |
| Run 返回 `scope_mismatch` | 请求 Scope 与 Session 不一致；使用 Session 绑定的 Scope |
| Evidence 详情 404 | Evidence 不属于当前 Scope，或 `content_hash` 不是该 Run 看到的版本 |
| 未配置 Key 时等待模型 | 正常情况下应快速进入离线回退；若卡住，检查日志中的 `model_skipped` / `model_error` |

## 10. 演示前检查清单

- [ ] `python -m unittest discover -s tests -q` 通过（当前基线 390 项）。
- [ ] `python -m compileall -q src benchmark tests` 通过。
- [ ] `/health`、`/demo/`、`/docs` 可访问。
- [ ] 演示数据库为合成数据，且未被 Git 跟踪。
- [ ] 浏览器中不出现 API Key、Authorization Header 或数据库堆栈。
- [ ] 所有 `insufficient` / `budget` / `model_error` 都被如实说明。
- [ ] 不声称真实模型调查成功率、`TaskSuccessRate` 或 Citation 语义支持已被验证。
