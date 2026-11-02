# ReAct 输出截断恢复调用修复

## 1. 失败报告

- 报告：`benchmark/reports/cross-atlas-synthesis-01`
- 任务：`cross-atlas`
- 模型：`deepseek-flash`（DeepSeek 官方 API）
- 终止：`termination_reason=budget`，`termination_detail=model_round_budget`
- 协议合法率：2/5（40%）
- 模型调用成功率：5/5；工具调用成功率：2/2；Evidence 召回 2/2；引用有效率 8/8
- 提交 Claim：0；接受 Claim：0
- 总 Token：输入 25022，输出 2729

## 2. Trace 逐调用结论

| 调用 | 结果 | finish_reason | 请求输出 | 实际输出 | reasoning | 说明 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `json_extra_data` | `stop` | 2048 | 63 | 0 | 不是单个 JSON 对象，未解析出 Action |
| 2 | `search_memory` | `stop` | 2048 | 49 | 18 | 合法工具调用，取得 5 条 Evidence |
| 3 | 工具执行 | — | — | — | — | 与调用 2 相同查询 |
| 4 | `json_syntax` | `stop` | 2048 | 357 | 277 | JSON 语法不完整，未解析出 Action |
| 5 | `search_memory` | `stop` | 2048 | 212 | 161 | 合法工具调用，`source_types=["git_commit"]`，取得 3 条 Evidence |
| 6 | 工具执行 | — | — | — | — | 与调用 5 相同查询 |
| 7 | `output_truncated` | `length` | 2048 | 0 | 2048 | reasoning 耗尽全部输出预算，正文为空 |

结论：

- 5 次模型调用中只有 2 次是合法工具调用；3 次是协议错误。
- 从未出现合法 `finish` 动作，因此不存在“模型提交了 Finish 但运行时拒绝”的情况。
- 证据数量已经足够（2/2 可接受 Evidence，8 个有效引用），但模型没有机会提交 Finish。
- 第 7 次调用符合 `finish_reason=length`，应触发 4096 Token 的截断恢复；旧循环在 `max_model_rounds=5` 用尽后直接退出，恢复调用从未发出。

## 3. 根因

这是可证明的状态机缺陷，不是 Claim/Evidence/引用校验被绕过：

1. 截断恢复原本只是“下一次循环迭代”的输出预算调整；它与普通调用共享 `max_model_rounds`。
2. 第 7 次调用是第 5 个常规决策轮。检测到 `output_truncated` 后，循环已无法进入下一次迭代。
3. 因此 4096 Token 重试被登记但未执行，运行以 `model_round_budget` 结束。

同一路径下，协议错误也会占用常规调查轮次。本次前两次 JSON 错误因此消耗了第 1、3 轮；这部分是模型未遵循协议，而不是运行时误解析。Prompt 与严格 Schema 本身明确，离线测试中合法 Finish 可正常通过；未保存原始模型文本，不能把前两次错误归因于 Prompt 不清晰。

## 4. 最小修复

文件：`src/agents/react_agent.py`

- 每个任务保留 **最多 1 次**“供应商确认输出截断（`finish_reason=length|max_tokens`）”恢复调用。
- 恢复调用不计入 `max_model_rounds`，只在 `truncation_recovery_pending=True` 时允许进入一次。
- 截断不再增加普通协议失败计数，避免被 `max_protocol_retries` 提前截断。
- 恢复调用仍受 `max_tokens`、`deadline_seconds` 和输出预算上限约束。
- 第二次截断立即安全终止：`termination_detail=output_truncated_after_recovery`，不生成 Claim。
- 恢复调用若返回合法工具调用而非 Finish，预算用尽后仍正常终止，不伪造 Finish。
- 所有 Claim 原文一致性、Evidence ID/hash、冲突检测、引用校验保持不变。

成本上限：

- 16 道任务、5 个常规轮：常规模型调用上限 80 次。
- 每题最多 1 次恢复调用：恢复调用上限 16 次。
- 模型调用硬上限 96 次（原 80 次），工具调用上限仍为 128 次。
- 单次请求输出上限在恢复时从 2048 提高到 4096；没有无限重试。

`benchmark/tasks/run_online.py`、`tests/test_task_eval.py`、`benchmark/tasks/README.md` 已同步记录该上限。

## 5. 回归测试

`tests/test_react_protocol.py`：

- `test_truncation_on_last_normal_round_gets_reserved_recovery_call`
  - 正常轮已用尽时发生截断，仍发出第 3 次调用于 4096 Token，随后合法 Finish 被接受。
- `test_truncation_recovery_is_not_blocked_by_protocol_retry_limit`
  - 两次协议错误后发生截断，恢复调用不被协议重试上限阻断。
- `test_truncation_recovery_is_bounded_to_one_call`
  - 恢复调用再次截断时只调用一次并安全终止，不产生 Claim。

离线全量结果：

```text
OPENAI_API_KEY= SILICONFLOW_API_KEY= DEEPSEEK_API_KEY= python -m unittest discover -s tests
Ran 219 tests in 19.061s
OK
```

## 6. 仍需真实模型验证

离线测试证明的是状态机行为与预算边界，不证明真实 DeepSeek 在该任务上一定成功：

1. 第 6 次调用使用 4096 Token 时，模型是否能在 reasoning 之后输出完整 Finish。
2. 模型前两次 JSON 错误是否会在同一 Prompt/Schema 下重现。
3. 真实供应商的 reasoning token 是否仍会耗尽 4096。
4. 任务语义是否正确仍需人工评分，不能由引用率或证据召回代替。

## 7. 在线验收命令（仅提供，未执行）

```powershell
$env:SILICONFLOW_BASE_URL = "https://api.deepseek.com"
$env:SILICONFLOW_MODEL = "deepseek-flash"
python -m benchmark.tasks.run_online `
  --online `
  --approved-origin https://api.deepseek.com `
  --task cross-atlas `
  --output benchmark/reports/cross-atlas-truncation-recovery-20261101
```

执行前需由操作者在当前进程环境中显式设置 `DEEPSEEK_API_KEY`；上面的命令不设置、不读取、不打印密钥，运行器也不自动加载 `.env`。该命令会产生真实模型费用。
