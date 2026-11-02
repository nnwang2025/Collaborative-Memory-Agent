# 评测标准权威性说明

日期：2026-11-01
范围：只读核对 `benchmark/tasks/labels.json` 与 `benchmark/labels/gold.json`。本阶段未修改任何标签，也未重新运行付费模型。

## 1. 两套标签分别属于哪个评测系统

| 文件 | 所属系统 | 用途 |
| --- | --- | --- |
| `benchmark/tasks/labels.json` | 任务级 Agent 质量评测（`benchmark/tasks/scoring.py`、`run_online.py`、`run_offline.py`、`grade.py`） | 任务事实标记、证据召回、引用身份、工具恢复、人工 `TaskSuccessRate` |
| `benchmark/labels/gold.json` | 冻结检索 / Gold Evidence 基准（`benchmark/dataset.py`、`benchmark/runner.py`、`benchmark/metrics.py`） | Gold Evidence ID/hash、预期行为、检索基准元数据 |

任务级运行报告中的 `TaskFactCoverage`、`EvidenceRecall`、`CitationValidity`、`TerminationReason` 由任务级评分读取 `benchmark/tasks/labels.json`。`gold.json` 不参与 `benchmark/tasks/scoring.py` 的任务判定。

## 2. `cross-atlas` 的终止原因不一致

- `benchmark/tasks/labels.json`：
  - `abstention.required = false`
  - `abstention.expected_termination = ["sufficient", "insufficient"]`
- `benchmark/labels/gold.json`：
  - `annotation_status = "pending_human_review"`
  - `expected_termination = ["sufficient"]`

两者确实不一致。原因不是运行结果异常，而是两套标签写于不同评测目的：

- 任务级标签允许“证据充分但未覆盖全部人工要点时保守回答为 `insufficient`”，并由人工规则判断答案是否把讨论理由与实现提交关联。
- 检索/Gold 基准只描述理想检索与预期行为，其 `expected_termination` 不是任务级通过/失败标准，且该条目仍标记 `pending_human_review`。

## 3. 权威标准

任务级在线/离线验收的权威标准是 `benchmark/tasks/labels.json`：

- 终止原因：`sufficient` 与 `insufficient` 都允许，不能因为 `insufficient` 自动判失败。
- 语义成功：只由人工 `TaskSuccessRate` 判定，不由 `TaskFactCoverage=1.0` 代替。
- 交叉来源要求：人工规则要求答案关联讨论理由与实现提交，并区分两类来源。

`benchmark/labels/gold.json` 只作为检索/Gold Evidence 的冻结参考，不作为任务级终止原因判定依据。

## 4. 未修改标签与版本隔离

本次没有修改 `labels.json` 或 `gold.json`，也没有为了让 `cross-atlas-02` 通过而放宽标准。

评分配置新增：

- `scoring_method_version = "cma-task-eval-method-v2"`
- `evaluation_authority.task_level = "benchmark/tasks/labels.json"`
- `evaluation_authority.retrieval_gold = "benchmark/labels/gold.json"`
- `comparability = "Do not compare scores across scoring_method_version or task_set_version changes without re-baselining."`

因此本次修复后的分数不能与 `cma-task-eval-method-v1` 的历史分数直接横向比较；历史在线报告保持原样，不被覆盖。
