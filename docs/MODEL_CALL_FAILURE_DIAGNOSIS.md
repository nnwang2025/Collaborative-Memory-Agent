# 第三次在线评测模型调用失败排查

日期：2026-11-01
范围：只读检查模型配置、客户端初始化与异常处理；仅修复明确的配置/诊断缺陷。未修改 ReAct 调查逻辑、AnswerAgent、Synthesis、Evidence 机制或评测标签；未执行 Git commit/push。

## 1. 已观察到的失败证据

来自 `benchmark/reports/third-batch-online-cross-atlas-03`：

- `execution_mode=online_model`
- `model_calls=1`
- `provider_output_tokens=0`
- `latency_ms=143.1`，Trace 中模型步骤 `elapsed_ms=125.0`
- `trace[0].action=model_decision`
- `trace[0].errors[0].error_code=model_error`
- `finish_reason=null`，`response_chars=null`
- `provider_input_tokens=5012` 是本地估算值，不是供应商返回的 usage
- `retryable=false` 是旧代码固定值，不代表真实可重试性

这些证据只能确定：**异常发生在模型调用内部，且在拿到可用响应/完成协议解析之前**。旧代码没有保留异常类别、HTTP 状态或供应商错误码，因此无法仅凭现有报告区分：

- DNS/TLS/连接失败；
- HTTP 401/403/404/429/5xx；
- HTTP 200 但响应不是预期 JSON；
- JSON 结构缺少 `choices`。

不能据此断言 `deepseek-flash` 不被支持。相同模型与 origin 在 `third-batch-online-cross-atlas-01/02` 中已有成功调用记录；本次失败本身不是模型名不受支持的证据。

## 2. 代码位置与实际配置优先级

### 2.1 实际 base URL / 模型 / Key

`src/agents/react_agent.py` 的 `ReActAgent.__init__` 读取：

| 配置 | 修复前优先级 | 默认值 |
| --- | --- | --- |
| base URL | `SILICONFLOW_BASE_URL` → `OPENAI_BASE_URL` | `https://api.siliconflow.cn/v1` |
| 模型 | `SILICONFLOW_MODEL` → `MODEL_NAME` | `Qwen/Qwen2.5-14B-Instruct` |
| API Key | `SILICONFLOW_API_KEY` → `OPENAI_API_KEY` | 无 |
| `DEEPSEEK_API_KEY` | **从未读取** | — |

`_call_llm()` 使用 `f"{self.base_url}/chat/completions"`。旧代码未去除 base URL 末尾斜杠。

### 2.2 在线运行器是否加载 `.env`

`benchmark/tasks/run_online.py` 修复前不调用 `load_dotenv()`；只有 `main.py` 和 `api.py` 会加载 `.env`。因此：

- 通过 `python -m benchmark.tasks.run_online` 运行时，**只认当前进程环境变量**；
- 只写在 `.env` 里的 Key 不会被该入口自动加载；
- 运行器的凭据存在性检查只看 `SILICONFLOW_API_KEY` / `OPENAI_API_KEY`。

只读检查当前 `.env` 的变量名（未读取、未打印任何 Key 值）：存在 `OPENAI_API_KEY`，未发现 `DEEPSEEK_API_KEY`；另有 `OPENAI_BASE_URL=https://api.siliconflow.cn/v1`、`MODEL_NAME=Qwen/Qwen2.5-14B-Instruct`。这与运行器中 PowerShell 设置的 `SILICONFLOW_BASE_URL` / `SILICONFLOW_MODEL` 是两套来源。

### 2.3 失败阶段

`run_result()` 的旧 `except Exception` 将所有模型调用异常统一压成：

```text
模型请求失败或响应无效，未暴露原始异常。
```

它没有设置 `finish_reason`，所以失败发生在 `_call_llm()` 内，而不是后面的 `parse_action()` 协议校验。具体是请求构建、HTTP 请求、响应解码还是响应结构阶段，旧报告无法区分。

## 3. 本次修复

### 3.1 安全诊断

`src/agents/react_agent.py` 新增 `model_error_diagnostic()` / `model_error_message()`：

- `phase`：
  - `http_request`：收到 HTTP 错误状态；
  - `transport`：DNS、连接、TLS、超时等；
  - `response_decode`：响应不是可解码 JSON；
  - `response_shape`：JSON 缺少 `choices` 等预期结构；
  - `request_build`：请求构建失败；
  - `unknown`：无法归类。
- `exception_type`：异常类名；
- `http_status`：仅 HTTP 状态码；
- `transport_reason`：只记录根异常类名，不记录异常文本；
- `provider_error_code`：只接受 `[A-Za-z0-9_.-]{1,64}` 的错误码；
- `provider_message`：最多 160 字，先替换实际 Key、Bearer 头、`sk-`/`ak-`/`key-` 形式与长 hex，再输出；
- `credential_source`：只记录实际使用的环境变量名，例如 `DEEPSEEK_API_KEY`，不记录值。

`ToolError` 新增可选 `diagnostic` 字段，写入 Trace 与 `traces/<task>.json`。不会写入请求体、Authorization 头或完整响应正文。

### 3.2 配置修复

- base URL 统一 `rstrip("/")`，避免 `https://api.deepseek.com//chat/completions`。
- 新增 `DEEPSEEK_API_KEY` 支持，但只在 DeepSeek origin 下优先：
  - DeepSeek origin：`DEEPSEEK_API_KEY` → `SILICONFLOW_API_KEY` → `OPENAI_API_KEY`；
  - 其他 origin：`SILICONFLOW_API_KEY` → `OPENAI_API_KEY`，**不会把 DeepSeek Key 发给其他供应商**。
- 新增 `DEEPSEEK_BASE_URL` 兜底，便于显式配置 DeepSeek；原有 `SILICONFLOW_BASE_URL` 优先级不变。
- 在线运行器凭据存在性检查加入 `DEEPSEEK_API_KEY`。
- **没有**自动加载 `.env`。在线运行仍要求操作者在当前 PowerShell 会话显式设置进程环境变量，避免离线测试或计划模式意外读取真实凭据并发起网络请求。

## 4. 离线测试

```powershell
$env:OPENAI_API_KEY = ""
$env:SILICONFLOW_API_KEY = ""
$env:DEEPSEEK_API_KEY = ""
python -m unittest discover -s tests
```

结果：

```text
Ran 216 tests in 18.696s
OK
```

新增回归（`tests/test_react_protocol.py`，全部使用 fake response / patched transport，无真实网络）：

- HTTP 401：记录 `phase=http_request`、`http_status=401`、`provider_error_code=invalid_api_key`、`credential_source=DEEPSEEK_API_KEY`；Key 与 Authorization 不出现在序列化结果中。
- `URLError(ConnectionRefusedError)`：记录 `phase=transport`，异常文本不进入结果。
- 非 JSON 响应：记录 `phase=response_decode`，响应正文不进入结果。
- 缺少 `choices`：记录 `phase=response_shape`。
- DeepSeek base URL 末尾斜杠被规范化，请求 URL 为 `https://api.deepseek.com/chat/completions`，payload 中模型为 `deepseek-flash`。

## 5. 仍不能确定的事项

- 本次 `cross-atlas-03` 的真实根因仍无法从旧报告反推；缺少安全 HTTP 状态和供应商错误码。
- 不能确认 `deepseek-flash` 当前的官方可用性；只能确认它在本次失败前曾被同一 origin 成功调用。
- 不能确认失败时实际使用的是 `SILICONFLOW_API_KEY` 还是 `OPENAI_API_KEY`，也不能确认其值是否属于 DeepSeek 账号。
- 新诊断只保证下一次失败可定位阶段/状态，不保证调用会成功。

## 6. 是否需要在 PowerShell 中改环境变量

需要。推荐在当前会话中显式设置：

```powershell
# 不要把 Key 回显到终端或写入报告/命令历史
$env:DEEPSEEK_API_KEY = "<your-deepseek-api-key>"
$env:SILICONFLOW_BASE_URL = "https://api.deepseek.com"
$env:SILICONFLOW_MODEL = "deepseek-flash"
Remove-Item Env:SILICONFLOW_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
```

说明：

- `run_online.py` 不会自动读 `.env`，必须在上面的 PowerShell 会话中设置。
- DeepSeek origin 会优先使用 `DEEPSEEK_API_KEY`；清除另外两个变量可避免误用旧 Key。
- `SILICONFLOW_BASE_URL` 不再需要末尾斜杠；代码已做规范化。
- `deepseek-flash` 是否可用仍需由真实响应判断，不能仅凭型号名假设。

## 7. 真实 DeepSeek 验证命令（仅提供，未执行）

```powershell
python -m benchmark.tasks.run_online `
  --online `
  --approved-origin https://api.deepseek.com `
  --task cross-atlas `
  --max-tasks 1 `
  --output benchmark/reports/third-batch-online-cross-atlas-modeldiag-20261101-01 `
  --max-model-rounds 1 `
  --max-tool-calls 1 `
  --max-output-tokens 2048 `
  --max-truncation-retry-output-tokens 4096 `
  --deadline-seconds 120
```

执行后查看：

- `benchmark/reports/third-batch-online-cross-atlas-modeldiag-20261101-01/traces/cross-atlas.json`
- `result.trace[*].errors[0].diagnostic`

若失败，应能看到 `phase`、`http_status`、`provider_error_code`、`credential_source` 之一；若成功，至少会看到有效的 `finish_reason` 与 provider usage。

## 8. 过程说明

实现过程中曾短暂加入“在线运行器自动加载 `.env`”，随后发现这会使一个使用 `clear=True` 的离线测试进入在线分支；该测试的目标 origin 是保留域名 `https://api.example.test`，不是真实供应商，且自动加载已立即移除。之后以显式清空三个 Key 环境变量的方式重跑全量测试。当前代码不再自动加载 `.env`。
