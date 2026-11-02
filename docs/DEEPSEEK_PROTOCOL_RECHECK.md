# 首次 DeepSeek 协议问题：当前工作区复核

这是对已有修复的再次核对，不是新的模型实验。当前代码已经包含 [协议修复](DEEPSEEK_PROTOCOL_FIX.md) 以及后续 [跨来源判据修复](DEEPSEEK_CROSS_SOURCE_FIX.md)，本次没有重复修改 Runtime、Prompt 或测试来制造新改动，也没有回退后续修复。

## 1. 原日志与根因复算

首次报告 `benchmark/reports/deepseek-smoke.json` 的 SHA-256 保持：

```text
725f4982004b0fdaef31ad7d423d48528b7efdea6bee92417848598270e8ea72
```

用当前 `src.smoke_diagnostics.diagnose()` 离线重新解析原报告，得到 32 个响应，其中 10 个通过 Schema、22 个协议错误：

| 分类 | 数量 |
| --- | ---: |
| JSON 语法错误（未闭合字符串、缺逗号等） | 14 |
| 空输出 | 3 |
| JSON 后追加说明或其他动作 | 2 |
| 非法 filters.source_types 枚举 | 3 |
| 其他 Schema 不匹配 | 0 |
| Trace 中 citation_error | 0 |

旧代码硬编码 `min(512, remaining_tokens - estimate)`，且丢弃 provider finish_reason，这是已确认的配置与观测问题。17 次错误响应达到 512 tokens，其中 14 次 JSON 语法错误、3 次空输出；但 32 个旧响应均缺失停止原因，不能将这些响应事后标记为已确认的 length 截断。0 次 citation_error 也不表示所有拟生成引用正确，因为多数 finish 根本没有进入引用验证。

insufficient 的 6 次错误为 3 次非法过滤枚举加 3 次 JSON 错误；tool_recovery 的 5 次错误为 4 次 JSON 错误加 1 次空输出。后者真实工具顺序是 search_issues、search_issues 重试、search_memory，不能误诊为不会换源。

四个 budget 场景均达到 7 次模型调用，总用量小于 90000、时长小于 120 秒、工具次数小于 8。结合旧循环逻辑，主要耗尽的是模型决策轮数，不能把它称作累计 Token 额度耗尽。

## 2. 当前实现核对结果

- `src/agents/react_agent.py`：请求输出上限取 Budget.max_output_tokens（默认 2048）和总剩余额度扣除输入估算后的最小值；保留 finish_reason、reasoning_tokens 和输出上限。
- `src/investigation.py`：分类诊断、字段路径脱敏、合法 source_types 和短 finish 示例齐全；最多 2 次连续格式修复，始终计入全局轮数、Token 与时间预算。
- `src/llm_smoke.py`：显式输出上限参数、响应元数据、协议错误分类及终止 detail 均保留。跨来源检查仍以实际来源和接受的引用为准，没有恢复为强制多工具。
- `tests/test_react_protocol.py`：既有 17 项协议回归完整保留，覆盖长 finish、已确认 length、512+stop 不误判、无 usage 元数据、非法过滤字段、合法 finish、换源、伪造 hash、格式重试及预算耗尽。
- Evidence ID/hash/scope、Ledger、原文摘录、零证据不作事实结论及只读工具约束没有改变。没有通过放宽验证掩盖错误。

首次协议修复涉及的完整文件与实现原因见原修复文档。本次新建的文件仅为本复核记录，不把既有修改谎称为本次新实现。

## 3. 已存在的第二次真实报告

第二次报告 `benchmark/reports/deepseek-smoke-protocolfix-20261026-01.json` 已存在，本次只读复核，SHA-256 为：

```text
ba05e27891c6b722f5b86c3fe3ae86c63428d87dec3e249279a0ec1094776bf6
```

其中共 17 个响应，全部记录 finish_reason=stop、requested_output_tokens=2048；五个场景协议错误均为 0，finish_accepted 均为 true。single_source、cross_source、multi_round 曾分别输出 634、550、808 tokens，说明该已保存运行没有继续使用旧 512 上限。

原报告状态仍是 4 个 passed、cross_source failed。后者是当时强制不同工具数的误判，已有独立修复与离线验证；本次没有把原报告改成 5/5，也不把这些协议指标当成语义任务成功率。第二次资料不能补回首次响应中丢失的停止原因。

## 4. 本次真实执行的离线验证

```powershell
python -m unittest discover -s tests -q
python -m benchmark.dataset
```

全量结果：**140 tests，140 通过、0 失败，17.760 秒，OK**。包含既有 17 项协议回归和 8 项跨来源回归，不重复添加等价测试。Benchmark 验证通过，仍为 cma-v1-fb8e82059066d95c、27 条证据、14 道测试和 2 道开发题；Decision Memory 回归包含在全量测试中。

本次没有新收费请求，没有读取、输出或修改 API Key，没有覆盖任何 JSON 报告，没有提交或推送 Git。旧日志重新分类仅在内存计算。

## 5. 使用新文件名重跑五场真实冒烟

`-01.json` 已经存在，不能再使用该名称。沿用操作人已有的 DeepSeek 进程配置，在明确同意费用和合成数据传输后手动执行以下命令；本次没有执行：

```powershell
python -m src.llm_smoke --online --approved-origin https://api.deepseek.com --max-output-tokens 2048 --max-protocol-retries 2 --output benchmark/reports/deepseek-smoke-protocolfix-20261026-02.json
```

本次检查时 -02.json 不存在；若之后已经创建，请换新后缀。脚本仍会在请求前拒绝覆盖已有报告，且不自动加载 .env 或输出 Key。

2048 不保证所有未来响应都完整。provider 缺失元数据、推理 Token 消耗、长 finish 或新的 Schema 偏离仍需按新报告实际诊断；不得凭 Token 数替代停止原因，也不能无限增加轮数或降低引用验证。单次合成冒烟不代表长期稳定性、通用语义正确性或跨数据集效果。
