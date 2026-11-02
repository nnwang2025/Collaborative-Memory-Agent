"""Prepare (but never execute) the small formal real-model acceptance run.

This module only writes experiment preparation material.  It contains no
provider key, makes no network request and has no code path that calls a model.
The task selection is fixed against the frozen Phase 4 snapshot; call caps are
derived from the frozen turn count.

Usage:
    python -m benchmark.v4.acceptance            # write benchmark/v4/acceptance
    python -m benchmark.v4.acceptance --plan-only
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
from typing import Any

from . import dataset
from .dataset import Snapshot

PLAN_VERSION = "cma-v4-acceptance-v1"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "benchmark/v4/acceptance"

SELECTION: tuple[tuple[str, str], ...] = (
    ("v4-single-source-01",
     "Clean single-source baseline: required facts must come from one source family "
     "without cross-source fusion."),
    ("v4-cross-source-01",
     "Cross-source task: the answer needs at least two source types, exercising "
     "multi-skill retrieval, merge and cross-source citation."),
    ("v4-multi-step-01",
     "Multi-step investigation: several constraints/entities must be resolved before "
     "the final fact can be supported."),
    ("v4-decision-history-01",
     "Historical decision cutoff: distinguishes a confirmed version at known_at from "
     "a later proposal that must not be reported as confirmed."),
    ("v4-conflict-01",
     "Unresolved conflict: both sides must be disclosed without the system choosing "
     "one side as the verdict."),
    ("v4-insufficient-01",
     "Insufficient evidence: the correct behaviour is a bounded abstention that "
     "discloses what is missing, not an invented answer."),
    ("v4-multi-turn-01",
     "Two-turn follow-up: tests per-turn evaluation targets and whether conversation "
     "context is used without treating the previous answer as evidence."),
    ("v4-tool-failure-01",
     "Injected document-tool timeout: tests recovery or explicit disclosure after a "
     "source tool fails."),
    ("v4-tool-failure-02",
     "Injected problem-tool timeout: a second, different failing skill, so the result "
     "does not depend on one failure point."),
    ("v4-budget-01",
     "Explicit budget override: tests whether the backend stops within the configured "
     "limit and discloses the unfinished part instead of claiming completion."),
)

_COMMON_CAPS = {
    "max_tasks": len(SELECTION),
    "max_tool_calls": 8,
    "max_model_calls": 80,
    "max_budget_tokens": 300_000,
}
CAPS = {
    "react": {**_COMMON_CAPS, "max_model_rounds": 5},
    "workflow": {**_COMMON_CAPS, "max_model_rounds": None},
}
SHARED = {
    "max_output_tokens": 1024,
    "max_retries": 1,
    "timeout_seconds": 120,
    "temperature": 0.1,
    "retrieval_mode": "hybrid",
}


def _turns(task: dict[str, Any]) -> int:
    return len(task.get("turns") or [task["question"]])


def _call_upper_bound(snapshot: Snapshot) -> dict[str, int]:
    turns = sum(_turns(snapshot.task(task_id)) for task_id, _ in SELECTION)
    return {
        "react": turns * (CAPS["react"]["max_model_rounds"] + 1) + turns,
        "workflow": turns * 3,
        "turns": turns,
    }


def _records_by_identity(snapshot: Snapshot) -> dict[tuple[str, str, str], Any]:
    return {(record.project_id, record.dataset_kind, record.evidence_id): record
            for record in snapshot.records}


def _ref_detail(snapshot: Snapshot, project_id: str, ref: dict[str, Any],
                by_identity: dict[tuple[str, str, str], Any]) -> dict[str, Any]:
    record = by_identity.get((project_id, "live", ref["evidence_id"]))
    return {
        "evidence_id": ref["evidence_id"],
        "content_hash": ref["content_hash"],
        "resolves": record is not None,
        "source_type": record.source_type if record else None,
        "source_uri": record.source_uri if record else None,
        "event_time": record.event_time.isoformat() if record else None,
        "ingested_at": record.ingested_at.isoformat() if record else None,
        "content_preview": (record.content[:180] if record else None),
    }


def _label_checklist(task: dict[str, Any], label: dict[str, Any],
                     snapshot: Snapshot,
                     by_identity: dict[tuple[str, str, str], Any]) -> dict[str, Any]:
    project_id = task["project_id"]
    groups = []
    for group in label.get("required_evidence_groups", []):
        groups.append({
            "group_id": group["group_id"],
            "any_of": [_ref_detail(snapshot, project_id, ref, by_identity)
                       for ref in group["any_of"]],
        })
    alternatives = []
    for alternative in label.get("acceptable_alternative_evidence", []):
        alternatives.append({
            "group_id": alternative["group_id"],
            "any_of": [_ref_detail(snapshot, project_id, ref, by_identity)
                       for ref in alternative["any_of"]],
        })
    return {
        "task_id": task["task_id"],
        "category": task["category"],
        "difficulty": task["difficulty"],
        "question": label.get("question") or task["question"],
        "turns": label.get("turns") or [],
        "known_at": label.get("known_at"),
        "required_evidence_groups": groups,
        "acceptable_alternative_evidence": alternatives,
        "expected_facts": label.get("expected_facts", []),
        "forbidden_inferences": label.get("forbidden_inferences", []),
        "expected_decision_state": label.get("expected_decision_state"),
        "expected_conflicts": label.get("expected_conflicts", []),
        "expected_abstention": label.get("expected_abstention"),
        "expected_limitations": label.get("expected_limitations", []),
        "injected_failure": label.get("injected_failure"),
        "budget_override": label.get("budget_override"),
        "human_review_rules": label.get("human_review_rules", []),
        "review_checks": [
            "Every required fact is semantically supported by the cited Evidence, not merely present as a string.",
            "Every accepted alternative Evidence is genuinely equivalent for this question.",
            "No forbidden inference appears, especially proposal-as-confirmed or unilateral conflict adjudication.",
            "Each citation resolves to the current content hash and the quoted claim appears in that Evidence.",
            "Abstention/limitation text is appropriate for the task and does not hide a wrong answer.",
            "Tool-failure and budget tasks disclose interruption rather than claiming completion.",
        ],
    }


def _experiment_config(snapshot: Snapshot) -> dict[str, Any]:
    upper = _call_upper_bound(snapshot)
    return {
        "experiment_id": PLAN_VERSION,
        "status": "prepared_not_executed",
        "this_round_made_model_calls": False,
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "dataset_version": snapshot.dataset_version,
            "task_set_version": snapshot.task_set_version,
            "task_set_sha256": snapshot.manifest["task_set_sha256"],
        },
        "task_ids": [task_id for task_id, _ in SELECTION],
        "backends": ["react", "workflow"],
        "same_controls_for_both_backends": [
            "frozen snapshot and task set", "project scope and Evidence database",
            "retrieval mode and verified index", "scorer", "provider/model/base URL",
            "identical hard model-call and token caps (architecture-derived upper "
            "bounds are recorded separately)",
        ],
        "provider_model": {
            "status": "operator_must_supply",
            "provider": "<exact provider name>",
            "model": "<exact priced model id>",
            "base_url": "https://<exact-origin>",
            "approved_origin": "https://<exact-origin>",
            "api_key_env": "<environment variable name only>",
            "note": "No key or .env file is read by this preparation step.",
        },
        "retrieval_mode": SHARED["retrieval_mode"],
        "index_preparation": {
            "required": True,
            "rule": "Build and verify one real FAISS index per project before either backend runs.",
            "abort_rule": "If preparation fails or any run reports bm25 fallback, abort the comparison; never score fallback as hybrid.",
            "expected": "ready",
        },
        "shared_limits": {
            "max_output_tokens": SHARED["max_output_tokens"],
            "max_retries": SHARED["max_retries"],
            "timeout_seconds": SHARED["timeout_seconds"],
            "temperature": SHARED["temperature"],
        },
        "per_backend_caps": CAPS,
        "call_upper_bound": upper,
        "cap_note": ("Caps are per backend run and shared across all selected tasks and "
                     "retries. Workflow has no native round concept; its cap is external. "
                     "ReAct's upper bound includes one truncation-recovery call per turn."),
        "token_and_cost_method": {
            "pre_run_estimate": ("calls_upper_bound * (estimated_input_tokens + "
                                 "max_output_tokens); estimated_input_tokens = UTF-8 bytes "
                                 "of the request JSON // 4. Treat as a planning ceiling, "
                                 "not a measurement."),
            "post_run_measurement": ("Use provider usage.prompt_tokens/completion_tokens "
                                     "when present; otherwise estimate bytes // 4 and mark "
                                     "estimated_usage=true."),
            "cost_formula": ("actual_cost_usd = prompt_tokens * input_unit_price + "
                             "completion_tokens * output_unit_price. Unit prices must be "
                             "supplied by the operator for the exact model; no price is "
                             "assumed here."),
            "this_round_cost": None,
            "provider_pricing_status": "not_supplied_no_cost_estimate",
        },
        "commands": {
            "react": ("python -m benchmark.v4.run_online --backend react --online "
                      "--i-understand-this-makes-paid-model-calls --provider <provider> "
                      "--model <model> --base-url https://<origin> --approved-origin "
                      "https://<origin> --retrieval-mode hybrid --task <id> [repeat] "
                      "--max-tasks 10 --max-model-calls 80 --max-budget-tokens 300000 "
                      "--max-model-rounds 5 --max-tool-calls 8 --max-output-tokens 1024 "
                      "--max-retries 1 --output benchmark/v4/reports/online-acceptance-react-<date>"),
            "workflow": ("python -m benchmark.v4.run_online --backend workflow --online "
                         "--i-understand-this-makes-paid-model-calls --provider <provider> "
                         "--model <model> --base-url https://<origin> --approved-origin "
                         "https://<origin> --retrieval-mode hybrid --task <id> [repeat] "
                         "--max-tasks 10 --max-model-calls 80 --max-budget-tokens 300000 "
                         "--max-tool-calls 8 --max-output-tokens 1024 --max-retries 1 "
                         "--output benchmark/v4/reports/online-acceptance-workflow-<date>"),
        },
        "comparability": {
            "comparable": ["retrieval quality", "structured fact accuracy",
                           "citation integrity", "latency and measured token use under the shared cap"],
            "incomparable": ["budget-protocol behaviour", "Claim Ledger validation",
                             "known_at filtering", "multi-round investigation depth"],
            "interpretation_rule": ("Do not attribute an observed ReAct/Workflow difference "
                                    "to ReAct reasoning as a cause. Report it as an end-to-end "
                                    "architecture difference."),
        },
    }


def build_plan(snapshot: Snapshot | None = None) -> dict[str, Any]:
    snapshot = snapshot or dataset.load()
    by_identity = _records_by_identity(snapshot)
    tasks = []
    for task_id, reason in SELECTION:
        task = snapshot.task(task_id)
        label = snapshot.label(task_id)
        tasks.append({
            "task_id": task_id,
            "category": task["category"],
            "difficulty": task["difficulty"],
            "selection_reason": reason,
            "turns": _turns(task),
            "injected_failure": label.get("injected_failure"),
            "budget_override": label.get("budget_override"),
        })
    categories = [item["category"] for item in tasks]
    required_categories = {"single_source", "cross_source", "multi_step",
                           "decision_history", "conflict", "insufficient",
                           "multi_turn", "tool_failure", "budget"}
    missing = required_categories - set(categories)
    if missing:
        raise ValueError("Acceptance selection is missing categories: " + ", ".join(sorted(missing)))
    if not 8 <= len(tasks) <= 12:
        raise ValueError("Acceptance selection must contain 8-12 tasks")
    checklists = [_label_checklist(snapshot.task(task_id), snapshot.label(task_id),
                                   snapshot, by_identity)
                  for task_id, _ in SELECTION]
    unresolved = []
    for checklist in checklists:
        groups = (checklist["required_evidence_groups"] +
                  checklist["acceptable_alternative_evidence"])
        for group in groups:
            for ref in group["any_of"]:
                if not ref["resolves"]:
                    unresolved.append(f"{checklist['task_id']}:{ref['evidence_id']}")
    if unresolved:
        raise ValueError("Gold references do not resolve in the frozen snapshot: "
                         + ", ".join(unresolved))
    return {
        "plan_version": PLAN_VERSION,
        "status": "prepared_not_executed",
        "snapshot_id": snapshot.snapshot_id,
        "task_set_version": snapshot.task_set_version,
        "task_count": len(tasks),
        "tasks": tasks,
        "coverage": {"categories": sorted(set(categories)),
                     "single_turn": sum(item["turns"] == 1 for item in tasks),
                     "multi_turn": sum(item["turns"] > 1 for item in tasks),
                     "tool_failure_tasks": sum(bool(item["injected_failure"]) for item in tasks),
                     "budget_tasks": sum(bool(item["budget_override"]) for item in tasks)},
        "experiment": _experiment_config(snapshot),
        "checklists": checklists,
    }


def _markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# Phase 4 小规模正式真模型验收准备", "",
        f"- plan_version: `{plan['plan_version']}`",
        f"- status: `{plan['status']}`",
        f"- snapshot: `{plan['snapshot_id']}`",
        f"- task_set: `{plan['task_set_version']}`",
        f"- tasks: {plan['task_count']}",
        "- 本轮未调用任何付费模型，未读取 API Key。", "",
        "## 任务选择", "",
        "| task_id | category | difficulty | turns | reason |",
        "|---|---|---|---|---|",
    ]
    for item in plan["tasks"]:
        lines.append(f"| {item['task_id']} | {item['category']} | {item['difficulty']} | "
                     f"{item['turns']} | {item['selection_reason']} |")
    lines.extend([
        "", "## 覆盖", "",
        f"- categories: {', '.join(plan['coverage']['categories'])}",
        f"- single-turn: {plan['coverage']['single_turn']}",
        f"- multi-turn: {plan['coverage']['multi_turn']}",
        f"- tool-failure tasks: {plan['coverage']['tool_failure_tasks']}",
        f"- budget tasks: {plan['coverage']['budget_tasks']}",
        "", "## 实验配置", "",
        f"- backends: {', '.join(plan['experiment']['backends'])}",
        f"- retrieval_mode: `{plan['experiment']['retrieval_mode']}`",
        f"- index: 每个项目构建并校验真实 FAISS；失败或回退即中止。",
        f"- provider/model: {plan['experiment']['provider_model']['status']}",
        f"- temperature: {plan['experiment']['shared_limits']['temperature']}",
        f"- max_output_tokens: {plan['experiment']['shared_limits']['max_output_tokens']}",
        f"- max_retries: {plan['experiment']['shared_limits']['max_retries']}",
        "", "| backend | max tasks | max model calls | max budget tokens | rounds |",
        "|---|---|---|---|---|",
    ])
    for backend, caps in plan["experiment"]["per_backend_caps"].items():
        lines.append(f"| {backend} | {caps['max_tasks']} | {caps['max_model_calls']} | "
                     f"{caps['max_budget_tokens']} | {caps['max_model_rounds']} |")
    upper = plan["experiment"]["call_upper_bound"]
    lines.extend([
        "", f"调用次数上限（规划上界）：ReAct {upper['react']}，Workflow {upper['workflow']}；"
        f"总 turn 数 {upper['turns']}。",
        "", "## Token 与成本估算方法", "",
        f"- 事前：{plan['experiment']['token_and_cost_method']['pre_run_estimate']}",
        f"- 事后：{plan['experiment']['token_and_cost_method']['post_run_measurement']}",
        f"- 成本：{plan['experiment']['token_and_cost_method']['cost_formula']}",
        "- 本轮无定价表，实际成本保持 `null`。",
        "", "## 可比性边界", "",
    ])
    lines.extend(f"- 不可比：{item}" for item in plan["experiment"]["comparability"]["incomparable"])
    lines.extend([
        "", "## 产物", "",
        "- `plan.json`：机器可读选择与配置。",
        "- `gold_review_checklist.md` / `.json`：逐任务人工复核清单。",
        "- `grading_template.csv`：`(task_id, backend)` 人工评分表，初始全部为空白。",
        "- `result_summary_template.md`：结果汇总模板。",
        "- `termination_recovery.md`：终止与恢复流程。",
        "- 评分表未经人工填写前，`TaskSuccessRate` 必须保持 `null`。", "",
    ])
    return "\n".join(lines)


def _checklist_markdown(plan: dict[str, Any]) -> str:
    lines = ["# Gold 人工复核清单", "",
             f"snapshot: `{plan['snapshot_id']}`；任务数：{plan['task_count']}。", "",
             "每个任务必须由至少一名 reviewer 独立核对。自动指标不能替代以下判断。", ""]
    for checklist in plan["checklists"]:
        lines.extend([
            f"## {checklist['task_id']} — {checklist['category']} ({checklist['difficulty']})", "",
            f"**问题**：{checklist['question']}", "",
        ])
        if checklist["turns"]:
            lines.append("**多轮目标**：")
            for index, turn in enumerate(checklist["turns"], start=1):
                lines.append(f"{index}. {turn.get('question')} → {turn.get('evaluation_target')}")
            lines.append("")
        if checklist["known_at"]:
            lines.append(f"**known_at 截止时点**：`{checklist['known_at']}`")
        if checklist["injected_failure"]:
            lines.append(f"**注入失败**：`{checklist['injected_failure']}`")
        if checklist["budget_override"]:
            lines.append(f"**预算限制**：`{checklist['budget_override']}`")
        lines.extend(["", "### 必要证据组", ""])
        for group in checklist["required_evidence_groups"]:
            lines.append(f"- `{group['group_id']}`")
            for ref in group["any_of"]:
                lines.append(f"  - `{ref['evidence_id']}@{ref['content_hash']}` "
                             f"source={ref['source_type']} event={ref['event_time']} — "
                             f"{ref['content_preview']}")
        if checklist["acceptable_alternative_evidence"]:
            lines.extend(["", "### 可接受替代证据（人工判断是否真的等价）", ""])
            for group in checklist["acceptable_alternative_evidence"]:
                lines.append(f"- `{group['group_id']}`")
                for ref in group["any_of"]:
                    lines.append(f"  - `{ref['evidence_id']}@{ref['content_hash']}` "
                                 f"source={ref['source_type']} — {ref['content_preview']}")
        lines.extend(["", "### 结构化事实", ""])
        for fact in checklist["expected_facts"]:
            kind = "必选" if fact.get("required") else "可选"
            lines.append(f"- [{kind}] `{fact.get('fact_id')}` {fact.get('statement')} "
                         f"values={fact.get('values')} markers={fact.get('marker_groups')}")
        if checklist["forbidden_inferences"]:
            lines.extend(["", "### 禁止推断", ""])
            for group in checklist["forbidden_inferences"]:
                lines.append(f"- 不得出现：{' / '.join(group)}")
        if checklist["expected_decision_state"]:
            lines.extend(["", "### 决策状态", "",
                          f"```json\n{json.dumps(checklist['expected_decision_state'], ensure_ascii=False, indent=2)}\n```"])
        if checklist["expected_conflicts"]:
            lines.extend(["", "### 冲突", "",
                          f"```json\n{json.dumps(checklist['expected_conflicts'], ensure_ascii=False, indent=2)}\n```"])
        if checklist["expected_abstention"]:
            lines.extend(["", "### 拒答/不足", "",
                          f"```json\n{json.dumps(checklist['expected_abstention'], ensure_ascii=False, indent=2)}\n```"])
        if checklist["expected_limitations"]:
            lines.extend(["", "### 必须说明的限制", ""])
            lines.extend(f"- {item}" for item in checklist["expected_limitations"])
        lines.extend(["", "### 人工复核", ""])
        lines.extend(f"- [ ] {item}" for item in checklist["review_checks"])
        if checklist["human_review_rules"]:
            lines.extend(f"- [ ] {item}" for item in checklist["human_review_rules"])
        lines.append("")
    return "\n".join(lines)


def _grading_csv(plan: dict[str, Any]) -> str:
    buffer = io.StringIO()
    fields = ["task_id", "backend", "category", "difficulty", "reviewer",
              "task_success", "answers_question", "required_facts_supported",
              "citations_support_claims", "conflict_disclosed",
              "decision_state_correct", "abstention_appropriate",
              "tool_failure_handled", "budget_limit_respected", "notes", "reviewed_at"]
    applicable = {
        "conflict_disclosed": "conflict",
        "decision_state_correct": "decision_history",
        "abstention_appropriate": "insufficient",
        "tool_failure_handled": "tool_failure",
        "budget_limit_respected": "budget",
    }
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for item in plan["tasks"]:
        for backend in plan["experiment"]["backends"]:
            row = {field: "" for field in fields}
            row.update(task_id=item["task_id"], backend=backend,
                       category=item["category"], difficulty=item["difficulty"])
            for field, category in applicable.items():
                if item["category"] != category:
                    row[field] = "n/a"
            writer.writerow(row)
    return buffer.getvalue()


def _summary_template(plan: dict[str, Any]) -> str:
    upper = plan["experiment"]["call_upper_bound"]
    return "\n".join([
        "# Phase 4 真模型验收结果汇总（模板）", "",
        f"- plan_version: `{plan['plan_version']}`",
        f"- snapshot: `{plan['snapshot_id']}`",
        f"- provider/model: `<填写>`",
        f"- run_id (React): `<填写>`",
        f"- run_id (Workflow): `<填写>`",
        f"- 执行日期: `<YYYY-MM-DD>`",
        f"- 是否发生 402/429/503: `<填写>`",
        f"- 实际模型调用: React `<n>` / Workflow `<n>`（规划上界 {upper['react']} / {upper['workflow']}）",
        f"- 实际 provider tokens: React `<n>` / Workflow `<n>`",
        f"- 实际成本: `<null 或按定价表计算>`", "",
        "## 运行状态", "",
        "| backend | selected tasks | completed | stopped/reason | model calls | tokens |",
        "|---|---|---|---|---|---|",
        "| react | 10 |  |  |  |  |",
        "| workflow | 10 |  |  |  |  |", "",
        "## 自动指标（不是 TaskSuccessRate）", "",
        "| metric | react | workflow | coverage |",
        "|---|---|---|---|",
        "| EvidenceRecall@K |  |  |  |",
        "| CitationValidity |  |  |  |",
        "| FactAccuracy |  |  |  |",
        "| AppropriateAbstentionRate |  |  |  |",
        "| ConflictDetectionRate |  |  |  |",
        "| DecisionStateAccuracy |  |  |  |",
        "| ToolFailureRecoveryRate |  |  |  |", "",
        "## 人工评分汇总", "",
        "| metric | react | workflow | 分母 |",
        "|---|---|---|---|",
        "| TaskSuccessRate |  |  | 10 |",
        "| Semantic citation support |  |  |  |",
        "| Conflict interpretation correct |  |  |  |",
        "| Decision cutoff correct |  |  |  |", "",
        "说明：TaskSuccessRate 仅在 20 个 `(task_id, backend)` 均有具名 reviewer 的显式判断后才计算。", "",
        "## 结果解读", "",
        "- 只报告同一模型、同一快照、同一任务集、同一 Scope、同一 scorer、同一预算上限下的端到端差异。",
        "- Workflow 在预算协议、Claim Ledger、known_at、调查轮次上不可比；不得把差异归因于 ReAct 推理机制的因果提升。",
        "- 任何索引回退、离线 fallback、402 中止或未完成运行都必须单列，不进入成功样本。",
        "- 语义支持、冲突解释和决策解释只能由人工判断，不能由自动分数替代。", "",
    ])


def _termination_markdown(plan: dict[str, Any]) -> str:
    return "\n".join([
        "# 终止与恢复流程", "",
        "## 运行前", "",
        "1. 确认 operator 已单独授权付费调用；准备精确 HTTPS origin、provider、model 和定价表。",
        "2. 不读取 `.env`；只在进程环境中设置所选 provider 的 Key 变量名。",
        "3. 两个 backend 使用同一份冻结快照、同一任务列表、同一 retrieval mode 和同一组上限。",
        "4. `hybrid` 索引必须真实构建并校验；任一项目失败即中止，不生成对照结果。", "",
        "## 运行中", "",
        "| 条件 | 行为 | 记录 |",
        "|---|---|---|",
        "| HTTP 402 | 立即终止整个 run，不再发起请求 | `stopped_insufficient_balance`, `http_402` |",
        "| HTTP 429/503 | 对当前任务最多重试 `max_retries=1` 次，指数退避 | 每次 attempt 与状态码写入 results |",
        "| `max_model_calls` | 在下一任务前停止，不越过硬上限 | `stopped_model_call_budget` |",
        "| `max_budget_tokens` | 在下一请求前停止 | `stopped_token_budget` / `budget_exhausted` |",
        "| 索引准备失败 | 中止，不写正式 summary | 异常退出，无结果目录 |",
        "| dense/hybrid 回退 | 中止，不把 BM25 结果记为 hybrid | `RetrievalChannelError` |",
        "| 工具注入失败 | 不终止 run；记录模型是否恢复或披露 | `failure_triggered`, trace |", "",
        "## 恢复", "",
        "- 只对未完成/失败任务续跑：`--resume <原输出目录>`。",
        "- 续跑要求配置指纹完全一致（含 cap、模型、origin、任务集）。修改 cap 或模型必须新建授权 run。",
        "- 已完成任务的结果行保留，不重复调用。",
        "- 402 中止后，只有 operator 确认账户/额度已恢复才可续跑。",
        "- 429/503 中止后，续跑前应检查 provider 状态；不得通过无限重试绕过限制。",
        "- 重新运行必须写入新目录；已有正式报告和已生成验收材料不被覆盖。", "",
        "## 本次准备边界", "",
        f"- 本目录生成于 plan `{plan['plan_version']}`。",
        "- 本轮没有读取 Key、没有发起请求、没有产生费用。",
        "- 实际执行需要单独授权，并遵守上表终止规则。", "",
    ])


def write(output: Path | str = DEFAULT_OUTPUT, *, snapshot: Snapshot | None = None,
          force: bool = False) -> dict[str, Any]:
    snapshot = snapshot or dataset.load()
    plan = build_plan(snapshot)
    output = Path(output)
    if output.exists() and not force:
        raise FileExistsError(f"Refusing to overwrite acceptance material: {output}")
    output.mkdir(parents=True, exist_ok=True)
    files = {
        "plan.json": json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        "README.md": _markdown(plan),
        "gold_review_checklist.md": _checklist_markdown(plan),
        "gold_review_checklist.json": json.dumps(plan["checklists"], ensure_ascii=False,
                                                 indent=2) + "\n",
        "grading_template.csv": _grading_csv(plan),
        "result_summary_template.md": _summary_template(plan),
        "termination_recovery.md": _termination_markdown(plan),
    }
    for name, content in files.items():
        (output / name).write_text(content, encoding="utf-8")
    plan["files"] = sorted(files)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.plan_only:
        print(json.dumps(build_plan(), ensure_ascii=False, indent=2))
    else:
        plan = write(args.output, force=args.force)
        print(json.dumps({"output": str(args.output), "tasks": plan["task_count"],
                          "status": plan["status"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
