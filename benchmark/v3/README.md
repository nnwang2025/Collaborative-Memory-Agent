# Phase 3 Context Engineering Benchmark (v3)

独立于 V1/V2 冻结数据的确定性上下文构造评测。**不调用真实 LLM、不读取 API Key、
不修改 `benchmark/` 下的历史 corpus/labels/reports。**

```powershell
python -m benchmark.v3.runner --output benchmark/v3/reports/<新目录>
python -m unittest discover -s tests -p "test_context_engineering.py" -v
```

## 评测臂

| 臂 | 说明 |
|---|---|
| `phase2_emulation` | 复现 Phase 2 请求形状：展示全部观察片段、最近两条消息、仅字节上限、无 token 预算。是显式仿真，不是历史报告复跑。 |
| `phase3` | 当前 `InvestigationContext.request()`：模型窗口 token 预算、结构化状态、优先级选择、去重、分层压缩、Trace 统计。 |

两臂使用同一批 Evidence、Ledger、Trace 和输入问题。

## 场景（12 类，13 行）

1. 少量短证据
2. 长上下文多来源
3. 重复证据/重复工具结果
4. 长文档多分块
5. 关键冲突低分证据
6. 已验证 Claim 与历史证据
7. 多轮追问
8. 无关会话历史干扰
9. 工具失败/重试信息
10. 极低预算（预期 `context_limit`）
11. 不同预算配置（roomy / tight）
12. 非精确 locator 摘录

## 指标

- 估算 prompt token、上下文占用率（token / 模型窗口）
- EvidenceRef 保留率
- 关键冲突 body 保留率
- 有效 Claim 支持证据保留率
- 重复内容比例
- `context_limit` 次数
- 上下文构造延迟
- 工具历史与缺口保真度
- 结构化状态字段（版本、verified claims、session 非事实标记）

任务成功率、语义正确性和 provider token 消耗只保留为 `null` 接口，脚本化结果不会
冒充真实模型效果。

## 输出

- `report.json`：逐行两臂指标、delta、聚合、限制
- `summary.md`：人类可读对比表

最近一次本机报告：`reports/phase3-context-20261102/`。

## 限制

- token 为保守估算，不是计费 token；
- Phase 2 臂是请求形状仿真，不代表原系统在真实模型下的表现；
- 未做语义评分或人工评分；
- 延迟为单次构造时间，未做统计重复；
- 会话上下文为构造输入，真实 Runtime 仍由 `ConversationContextBuilder` 做字节上限。
