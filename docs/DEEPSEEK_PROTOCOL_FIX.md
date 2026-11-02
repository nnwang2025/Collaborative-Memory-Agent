# DeepSeek Flash 首次冒烟：协议诊断与修复

日期：2026-10-26。本轮仅分析已有报告、修改协议实现并执行离线测试，没有重新调用收费模型，没有读取或输出 API Key、加载 .env、覆盖旧报告或提交/推送 Git。

## 1. 原始证据与诊断边界

原始报告：[deepseek-smoke.json](../benchmark/reports/deepseek-smoke.json)，模型 `deepseek-flash`，origin 为 `https://api.deepseek.com`。文件 SHA-256：

```text
725f4982004b0fdaef31ad7d423d48528b7efdea6bee92417848598270e8ea72
```

只读取其中的动作、Trace、配置、用量和错误等诊断字段，未读取任何凭据配置。新分析另存为 [deepseek-smoke-diagnosis-20261026.json](../benchmark/reports/deepseek-smoke-diagnosis-20261026.json)，保留原始文件及其 hash。

检查旧 ReActAgent 发现两个确定的问题：

1. `run_result()` 每轮用 `min(512, remaining_tokens - estimate)` 设置输出上限，`_model_limits` 默认也是 512；`_call_llm()` 将该值作为请求的 `max_tokens`。总预算 90000 **不是**单次输出预算。
2. `_call_llm()` 只返回 content 和 usage，丢弃 `choices[0].finish_reason`；无 usage 时更退化为单独字符串。因此旧报告没有 provider 的停止原因，无法事后恢复。

旧日志中 17 次输出恰好为 512 tokens，包含 14 条字符串/JSON 未闭合和 3 次空 content。结合已确认的请求上限，输出受限嫌疑很强；但**不能认定这 17 次全部都是服务端 finish_reason=length**。空 content 也可能与 provider 推理输出或其他响应行为有关，旧日志没有相应字段，不能补造结论。

`AnswerAgent` 的另一个 512 上限属于固定 Workflow 文本总结路径，本次 ReAct 的 finish 不经过该请求，因此本轮没有修改那个独立路径，也没有重写检索或 Workflow。

## 2. 逐类归因

5 场共 32 个响应，10 个通过 Action Schema，22 个协议错误。以下是互斥的**直接解析分类**，不是用 Token 数推断的服务端原因：

| 类型 | 次数 | 真实证据 |
| --- | ---: | --- |
| JSON 语法错误 | 14 | 字符串未闭合、缺少逗号/值等；全都 output_tokens=512 |
| 空输出 | 3 | content 为空；output_tokens=512，finish_reason 未记录 |
| JSON 后追加内容 | 2 | cross_source 第 1 响应、multi_round 第 1 响应，json.loads 报 Extra data；分别只用 121、124 output tokens |
| 非法过滤枚举 | 3 | insufficient 第 1、2、4 响应，filters.source_types 使用 decision、discussion、doc、design_doc、architecture 等非法值 |
| 其他 Schema 不匹配 | 0 | 旧报告未观察到；新诊断器支持单独分类 |
| Ledger/hash/scope 引用校验异常 | 0 | 旧 Trace 没有 citation_error；多数 finish 在 JSON 解析前就失败，不能由此宣称所有拟生成引用都正确 |

source_types 原 Schema 仅接受 `git_commit / issue / document / chat / log / code / memory / unknown`，但旧 Prompt 只列出了字段名，没有列合法枚举；Runtime 又只反馈笼统“请输出合法 JSON”，模型无法据此定位具体枚举错误。这是协议说明和反馈不足，不应归为模型能力不足。

| 场景 | 调用数 | 协议错误构成 | 原终止 |
| --- | ---: | --- | --- |
| single_source | 4 | 1 次空输出 | insufficient |
| cross_source | 7 | 1 次 Extra data + 4 次 JSON 语法错误 | budget |
| multi_round | 7 | 1 次 Extra data + 3 次 JSON 语法错误 + 1 次空输出 | budget |
| insufficient | 7 | 3 次非法过滤枚举 + 3 次 JSON 语法错误 | budget |
| tool_recovery | 7 | 4 次 JSON 语法错误 + 1 次空输出 | budget |

tool_recovery 的真实工具顺序为 `search_issues → search_issues（有限重试）→ search_memory`。模型确实完成了故障后换源；后续 5 个 finish 尝试没有通过 JSON 解析，不能把这次失败描述为“不会换源”，也不能误称引用校验失败。

single_source 的最后一个 finish 通过 Schema 且没有 citation_error，模型保留了后续状态等缺口，结果为 insufficient。其 smoke status=failed 也受前面一次 protocol_error 的严格冒烟判据影响，不能等同于所有动作都失败。

四个 budget 场景均已经调用 7 次模型；总 Token 分别为 15422、17139、12179、15230，远小于 90000，时长约 17.9–18.7 秒，远小于 120 秒，工具尝试也未达 8 次。结合循环 `for ... else: reason='budget'`，本次主要耗尽的是 **7 轮模型决策额度**，格式重试消耗了正常调查轮次。

## 3. 实施的最小修复

### 请求与停止原因

- 在现有 Budget 中增加 `max_output_tokens`，默认 2048，可配置 128–16384。
- 每次真正请求上限为 `min(max_output_tokens, 总剩余额度 - 本轮输入保守估算)`，保留总 Token、轮数、工具数和 deadline 约束，没有额外隐藏调用。
- `_call_llm()` 总是返回 ModelReply，保留 provider `finish_reason`、请求输出上限、可用的 reasoning_tokens 和实际 usage；缺 usage 时仍保留停止元数据，由 Runtime 标记估算用量。
- `finish_reason=length/max_tokens` 分类为 output_truncated，不执行其中任何动作，即使 JSON 外观完整；不拼接残片、不补全 JSON、不从 reasoning_content 提取动作。
- output_tokens=512 且 finish_reason=stop 的合法动作正常接受；未提供停止原因时不会仅凭 Token 数认定截断。

### 协议、错误反馈和有限修复

- Prompt 明确 source_types 枚举，要求严格单个 JSON；finish 优先至多 2 条短原文摘录、1–3 个短缺口，省略空数组与默认字段。完整 ID/hash 不能省略。
- 这只是紧凑输出指引，没有收紧旧合法 finish 的 Schema 上限；长的合法 finish 仍可解析，以保持兼容。
- 提供最小合法不足响应：`{"action":"finish","claims":[],"gaps":["缺少生产状态证据"]}`，不强迫模型在无证据时填充事实。
- 分类为 output_truncated / empty_output / json_extra_data / json_syntax / invalid_filter / schema_mismatch / unexpected_finish_reason；citation_error 继续独立处理。
- Pydantic 错误反馈仅含允许的字段路径和错误类型，不回显原始值、验证上下文、私有正文或模型的非法输出；未知额外字段名也脱敏，附合法过滤说明和短 finish 示例。
- 新增 `max_protocol_retries=2`：首次错误后最多 2 次连续格式修复，即连续第 3 次错误就停止；合法动作后重置连续计数。重试始终计入原模型轮次和 Token 预算。
- 终止 detail 区分 model_round_budget、tool_call_budget、token_budget、deadline、protocol_retry_limit、model_error、citation_validation_failed、finish_validated。原有 termination_reason 枚举保持兼容；格式重试上限提前停止为 insufficient，不冒充 sufficient。

### 冒烟观测

Trace 新增 finish_reason、requested_output_tokens、response_chars、reasoning_tokens、protocol_category、validation_issues。仅保存诊断元数据，不把原始模型输出加入默认 Trace。

显式在线合成冒烟仍单独保存原有 Actions，并增加 model_responses、分类计数和 Budget 配置。finish_accepted 表示最终协议/引用阶段接受结束申请；protocol_recovered 表示发生格式错误后最终接受了结束申请。两者不表示语义任务成功，task_success 仍待人工判断。

冒烟 status 仍采用严格判据，出现过协议错误即使最终修复也不会悄悄抹去失败；新增字段便于区分“最终协议恢复”与“全程无协议错误”。预算耗尽不能算不足场景成功。

## 4. 没有放松的约束

- `extra=forbid`、合法工具/过滤枚举、日期时区及范围不变；不把 doc 等非法枚举偷偷转成合法来源。
- Evidence ID/hash 格式、当前 Ledger 已观察身份、project/dataset scope、原始来源回取及逐字摘录校验不变。
- 零证据不输出已确认事实；当前生产有效性仍不能由历史记录或内部 Decision Memory 直接证明。
- JSON 后有额外文本或第二个动作，仍整体拒绝，不“捞出第一个对象”执行工具。
- citation_error 不因格式修复机制被放行；原有拒绝或降级行为保留。
- 所有工具保持只读；Decision Memory、Benchmark 冻结文件和数据库模型均未改动。

## 5. 离线验证

新增 `tests/test_react_protocol.py`，测试使用本地临时 SQLite、脚本响应和截获的 HTTP 传输，**不是重新执行真实 DeepSeek 测试**。覆盖：

- 请求输出上限 2048 与剩余总预算扣减；真实解析请求 payload 和 provider 响应格式。
- 超过旧输出大小的合法长 finish，并保留完整 ID/hash。
- provider length 截断、外观完整但 length 的响应、缺 usage 时保留停止原因。
- output_tokens=512 但 stop 时不能误判；空输出缺停止原因时只归 empty_output。
- 非法过滤字段/枚举的脱敏精确反馈，scope 不变；其他 Schema 错误和异常停止原因独立分类。
- Extra data 不被自动抢救执行；连续格式修复、模型轮数和总 Token 耗尽均受限。
- Issue 超时后实际调用 search_memory，再从截断 finish 恢复到合法 finish。
- 格式合法但 hash 伪造仍 citation_error；语法诊断与可信引用校验分开。
- 冒烟报告保留停止元数据，语义任务状态仍未评分。

全量实际结果：**132 项通过、0 失败，12.195 秒，OK**。原 115 项测试保留，新的 17 项不依赖 API Key。

```powershell
python -m unittest discover -s tests -q
python -m benchmark.dataset
python -m compileall -q src tests
```

上述命令均 exit 0。`git diff --check` 也 exit 0，仅提示已有工作区的 LF/CRLF 转换，没有空白错误。新诊断 JSON 已重新计算核对一致，原始报告 SHA-256 与分析前完全相同。

Benchmark 验证已通过，仍为 `cma-v1-fb8e82059066d95c`，27 条证据、14 道测试、2 道开发题，标签待人工复核。原有结果保留为历史运行，不能因修改代码而重写其 source hash。

可单独复现**不联网的旧日志诊断**，输出应使用另一个新文件名：

```powershell
python -m src.smoke_diagnostics benchmark/reports/deepseek-smoke.json --legacy-output-limit 512 --output benchmark/reports/deepseek-smoke-diagnosis-new.json
```

诊断器只分析已保存资料，不创建模型客户端，旧文件缺失的 finish_reason 会明确保留为 null。legacy-output-limit 是从旧代码核对出的参数，不能当作 provider 停止原因。

## 6. 下一次五场真实测试命令（本轮未执行）

沿用操作人已配置的 DeepSeek 进程环境；此脚本不会自动加载 .env 或设置/显示 Key。需由操作人在明确同意服务费用与合成数据传输后执行：

```powershell
python -m src.llm_smoke --online --approved-origin https://api.deepseek.com --max-output-tokens 2048 --max-protocol-retries 2 --output benchmark/reports/deepseek-smoke-protocolfix-20261026-01.json
```

该报告名本轮没有创建；若执行时已存在，脚本会在请求前拒绝，应改为新的后缀。approved-origin 必须匹配进程中实际生效的 HTTPS base URL；脚本不会跟随重定向、覆盖旧报告或修改配置。实际模型名称会写入新报告，不能假设仍是旧模型。

重新运行时优先检查每次 finish_reason、requested_output_tokens、reasoning_tokens、分类反馈和最终 finish_accepted；然后人工核对答案。2048 仍可能不足，provider 也可能返回缺少元数据的响应，或继续生成非法 Schema。可以显式调高上限，但应先看停止原因，不能无限增加轮次或放松引用验证。

## 7. 本轮文件清单及未验证部分

| 文件 | 修改目的 |
| --- | --- |
| src/agents/react_agent.py | 输出预算、停止元数据、Prompt、分类反馈、有限修复与终止 detail |
| src/investigation.py | 兼容新增 Budget/ModelReply/Trace 字段，安全的协议分类与反馈 |
| src/llm_smoke.py | 输出上限 CLI 和完整响应遥测、最终协议状态 |
| src/smoke_diagnostics.py | 新增离线旧日志分类器 |
| tests/test_react_protocol.py | 新增 17 项离线回归 |
| benchmark/reports/deepseek-smoke-diagnosis-20261026.json | 新增分类结果，不改原报告 |
| docs/DEEPSEEK_PROTOCOL_FIX.md、README.md | 诊断证据、边界、验证与重跑命令 |

未验证：修复后的真实 DeepSeek 响应质量、provider 是否总提供 finish_reason、增加输出上限后的实际延迟/费用、空 content 是否涉及推理 Token 消耗。没有新线上成功率，也不能用离线脚本响应证明模型效果提高。
