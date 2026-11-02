# 独立评测

本目录与演示 mock 数据、旧 src/evaluate.py 题库分离。27 条合成 Evidence、14 道测试题（七类各 2）、2 道独立项目开发题；全部标签待独立人工复核。使用 live 通路，不代表生产数据。

> **当前任务级评测请看 [tasks/README.md](tasks/README.md)**：它是独立的 16 任务 / 8 类别评测层（`cma-task-eval-v1`），不替换本目录的 14 题冻结 Benchmark。历史报告与标签均保持原样。

```powershell
python -m benchmark.dataset
python -m benchmark.runner --split dev --output benchmark/reports/dev-new
python -m benchmark.runner --split test --output benchmark/reports/test-new
python -m unittest discover -s tests -v
```

输出目录必须不存在。Runner 仅离线运行真实 Workflow 与 ReAct 无 Key fallback，阻止模型网络请求；每题/后端用独立临时 DB。不要将 fallback 结果当真实 LLM 对比。正式本轮报告使用 reports/offline-final。

corpus 为原始证据、审核历史 SQL 和显式冲突关系；cases/public.json 是可公开问题，labels/gold.json 是评测侧隐藏标准。manifest.json 冻结这三类文件。Agent 仅接收公开问题与 scope 和运行用 corpus DB，不接收隐藏标签。

作者脚本只用于创建**新快照目录**，不在评测时生成标准答案：

```powershell
python -m benchmark.scripts.build --output <不存在的新快照目录>
```

生成后需独立复核，不应覆盖本快照来迎合已有结果。测试验证同环境种子确定性；跨 OS 的 Git 重新生成一致性尚未验证。

人工阅读原始结果、证据及标签后，单独写 JSON 列表，每项绑定原结果的 run_id / case_id / backend，以及 reviewer（实际评审者）、task_success（布尔）、可选 notes 和 appropriate_abstention（布尔）。不填写默认成功、不为 unsupported 打成功分。本轮没有实际人工评审文件。

```powershell
python -m benchmark.scripts.grade --report benchmark/reports/offline-final --grades <人工评审JSON路径> --output <新报告目录>
```

人工评分不重跑 Agent、不修改 corpus/labels 或原始报告。无评分的语义指标保持 null。完整数据、指标分母、实验限制和失败案例见 [BENCHMARK_EVALUATION.md](../docs/BENCHMARK_EVALUATION.md)。
