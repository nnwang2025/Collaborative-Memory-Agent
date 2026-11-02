# 第二次 DeepSeek 冒烟 cross_source 误判修复

本轮只读取已有报告、修改冒烟评测和新增离线测试，没有请求收费 API、读取/显示/修改 API Key 或覆盖既有报告，没有 Git commit/push。

## 1. 失败条件与真实执行证据

原始报告：[deepseek-smoke-protocolfix-20261026-01.json](../benchmark/reports/deepseek-smoke-protocolfix-20261026-01.json)。读取前后 SHA-256 相同：

```text
ba05e27891c6b722f5b86c3fe3ae86c63428d87dec3e249279a0ec1094776bf6
```

cross_source 的原问题是：

> Investigate Cedar decision reasons in commits and its later review in documents; cite both.

原 `src/llm_smoke.py::run_suite()` 在通用协议检查后额外要求：

```python
len(set(order)) >= 2 and len(result.evidence_refs) >= 2
```

该次结果有 2 次模型响应、1 次 search_memory、4 个最终 Ledger 引用、2 条接受的历史 Claim；协议错误为 0，finish_accepted=true，结束原因为 insufficient。通用检查通过、引用数量满足，唯一不满足的分支条件是不同工具数只有 1。

`ScopedSkill.SOURCE_TYPES['memory'] = None`，其 live 路径按显式 filters 检索多种来源。本次工具请求的 source_types 就是 git_commit 和 document。因此，一个 search_memory 完全可以同时获得 Commit 与文档；工具数量不能代替来源覆盖。

报告保存的回答、已接受 Claims 和对应 ID/hash 表明覆盖了两项需求：

| 需求 | 实际已接受的原文引用 | 对应 Evidence |
| --- | --- | --- |
| Commit 中的决策原因 | Cedar chose B due to bounded latency | ev_60bfd8d2ecec4053543f91ec2283f58c1fb95fae9f110cce02f91f00b4b335c3 |
| 文档中的后续评审 | Cedar review proposes adjusted timeout. Production status unknown. | ev_2493352ba71bb7d297fccc90c2c81b08368782bca78433fffd0c0206cc409ff7 |

第一条在来源展示中对应 2025-02-01 决策提交，第二条对应 cedar.md 文档。另有初始提交与后续 timeout 提议提交被检索。source_uri 都可能是相同仓库 URI，所以也不能用不同 URI 数量机械判断跨来源。

旧报告没有保存完整结构化 Evidence，原运行临时 DB 已清理。本次不是对这些旧 ID 的独立数据库重新回取，而是依据原报告接受的 Claim、Trace 和来源展示定位误判，并在新临时 Git/SQLite 中验证修复后的真实链路。没有重写原结果或声称重新运行真实 LLM 得到 5/5。

## 2. 原问题是否答全、insufficient 是否合理

原问题要求提交里的采用原因，以及文档里的后续评审，并引用双方。两条已接受原文分别提供了选择 B 的 bounded latency 原因和后续 adjusted timeout 提议，覆盖该合成场景明确要求的两项事实。

模型额外说明了生产采用/效果和 timeout 提议更深层原因缺少证据。原始数据确实没有生产部署记录，文档仅描述提议；这些未知项不能被补成事实。模型设 requires_current_status=true，Runtime 保留缺口，并按现有规则返回 insufficient。这与已有历史事实能被引用并不矛盾。

原 cross_source 判据也没有要求 termination_reason=sufficient。因此，本次 failed 不是由 insufficient 引起。没有证据表明必须再调用第二个工具才算完成本场景，不应强制无意义的工具调用、删除合理缺口，或将 Runtime 结束状态改成 sufficient 来提高通过率。本轮不修改 ReActAgent、AnswerAgent 或调查终止逻辑。

这只能说明本场合成问题的事实与来源覆盖，不能据此给通用语义整合、因果推理能力打满分；task_success 仍为 null，task_grading 保持 pending_human_review。

## 3. 最小修复

新增 `check_cross_source_evidence()`，只在冒烟 cross_source 分支调用，替换“至少两个工具”条件：

1. 从结果的 Ledger 引用集合出发，确认 ID 确实出现在成功 tool_attempt 中。
2. 用相同 scope 和精确 content_hash 从当前临时 EvidenceStore 回取；不接受不存在或 legacy 记录。
3. 只统计最终接受的 historical Claims；每个引用必须属于上述实际证据集合，且 Claim 文本仍是来源原文子串。
4. 对合成场景的两个既定事实角色分别检查：Git Commit 的 bounded latency 原因、document 的 adjusted timeout 评审。取短事实片段，不要求模型复制整句，防止把措辞长度变成新误判。
5. 两个角色均被引用覆盖、scope 一致、没有不可核验引用或冲突时，通过该场景的证据覆盖检查。不要求不同工具，也不强迫 sufficient。

角色内容来自既有 prepare() 合成资料，不改问题、语料、Agent Prompt 或 Benchmark 标签；它们只用于运行结束后的固定 fixture 检查，不传入模型。

报告增加 cross_source_check，明确保存规则版本、角色覆盖、缺失项、实际检索/引用的 source_types，以及已核验引用的 ID/hash/scope/source_uri/event_time。缺失事件时间继续为 null。取得两种来源但未在回答中引用双方，不算通过。

这是具备来源校验的合成证据覆盖判据，不是通用语义评分器。原本的 finish、协议、citation_error 和预算检查继续执行；Evidence ID/hash/Ledger/scope/原文校验没有放松。

## 4. 文件与验证

| 文件 | 原因 |
| --- | --- |
| src/llm_smoke.py | 按实际引用来源和场景事实覆盖判定，增加可解释检查结果 |
| tests/test_smoke_cross_source.py | 新增 8 项离线回归，使用真实临时 Git、SQLite、Skills 和 ReAct Runtime |
| docs/DEEPSEEK_CROSS_SOURCE_FIX.md | 保存诊断依据、修复范围和验证结果 |

新增测试覆盖：单工具双来源并允许不足状态、短原文摘录、只检索不引用、不包含决策原因、多个工具但单一来源、伪造 hash/跨项目/跨数据集/未观察引用、零证据/非原文，以及真实 smoke runner 接入新判据。

其中模型响应为脚本数据，HTTP 传输被阻断；测试不是新的真实模型结果。测试报告只写入临时目录。

```powershell
python -m unittest discover -s tests -q
python -m benchmark.dataset
python -m compileall -q src/llm_smoke.py tests/test_smoke_cross_source.py
git diff --check
```

实际全量结果：**140 tests，140 通过，0 失败，18.830 秒，OK**；保留此前 132 项，新增 8 项。其他命令均 exit 0，Git 仅提示现有 LF/CRLF 转换。Benchmark 保持快照 cma-v1-fb8e82059066d95c：27 条证据、14 道测试、2 道开发题，原有 Decision Memory 测试通过。

## 5. 是否必须重新付费测试

**验证本次判据修复不需要重新调用收费模型。** 原报告已经足以定位工具数误判，离线测试覆盖了真实数据和工具链，且没有修改 Agent 的模型请求或策略。

若希望取得使用新判据生成的正式在线报告，可由操作人之后明确授权并使用新文件名执行；这属于可选的新实验，不是本轮验证的必要条件。本轮没有生成新的线上通过率，也没有将旧报告的 failed 改成 passed。
