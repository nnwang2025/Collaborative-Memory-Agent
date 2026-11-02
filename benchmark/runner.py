"""Offline adapters run real backends, without labels, model calls or oracle actions."""
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from benchmark.dataset import ROOT, load, materialize, validate
from benchmark.metrics import aggregate
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.agents.answer_agent import AnswerAgent
from src.agents.coordinator_agent import CoordinatorAgent
from src.agents.react_agent import ReActAgent, REACT_SYSTEM_PROMPT
from src.skills.registry import ScopedSkill


class PublicCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    category: str
    question: str
    project_id: str
    dataset_kind: str
    corpus_snapshot_id: str
    split: str


TOOL_NAMES = {"memory": "search_memory", "problem": "search_issues", "document": "search_docs",
              "dependency": "analyze_dependency", "entrypoint": "find_entrypoint"}


def run_backend(case, runtime, backend, injected_failure=None):
    """No gold IDs, expected claims, grading notes, labels or sibling results here."""
    case = PublicCase.model_validate(case)
    evidence_path, memory_path = runtime
    scope = EvidenceScope(project_id=case.project_id, dataset_kind=case.dataset_kind)
    events, captures, captured_memories = [], [], []
    lock = threading.Lock()
    original_run, original_compose = ScopedSkill.run, AnswerAgent.compose
    def observe(skill, question, *args, **kwargs):
        start = time.perf_counter()
        event = {"tool_name": TOOL_NAMES[skill.name], "query": question, "filters": str(kwargs.get("filters", "")),
                 "evidence_ids": [], "error": None}
        try:
            if injected_failure and skill.name == injected_failure["skill"]:
                event["error"] = "injected_timeout"
                raise TimeoutError("Synthetic benchmark source unavailable")
            result = original_run(skill, question, *args, **kwargs)
            event["evidence_ids"] = [e["evidence_id"] for e in result["evidence"]]
            event["evidence_refs"] = [{"evidence_id": e["evidence_id"], "content_hash": e["content_hash"]} for e in result["evidence"]]
            return result
        finally:
            event["elapsed_ms"] = (time.perf_counter() - start) * 1000
            with lock:
                event["order"] = len(events) + 1
                events.append(event)
    def compose(agent, question, skill_names, skill_results, route_scores=None):
        all_hits = [dict(item, skill=name) for name in skill_names for item in skill_results.get(name, {}).get("evidence", [])]
        captures.extend(agent._top_relevant(question, all_hits)[:8])
        captured_memories.extend(m for name in skill_names for m in skill_results.get(name, {}).get("decision_memories", []))
        return original_compose(agent, question, skill_names, skill_results, route_scores)
    env = {"DEEPSEEK_API_KEY": "", "DEEPSEEK_BASE_URL": "", "DEEPSEEK_MODEL": "",
           "SILICONFLOW_API_KEY": "", "SILICONFLOW_BASE_URL": "", "SILICONFLOW_MODEL": "",
           "OPENAI_API_KEY": "", "OPENAI_BASE_URL": "", "MODEL_NAME": "",
           "EVIDENCE_DB_PATH": str(evidence_path), "DECISION_DB_PATH": str(memory_path)}
    result = {"run_id": str(uuid4()), "backend": backend, "mode": "offline", "answer": "", "runtime_error": None,
              "model_calls": 0, "provider_input_tokens": 0, "provider_output_tokens": 0, "estimated_tokens": 0,
              "termination_reason": None, "citation_refs": [], "decision_memories": [], "invalid_actions": 0,
              "duplicate_calls": 0, "action_tool_requests": 0}
    start = time.perf_counter()
    with patch.dict(os.environ, env), patch("urllib.request.urlopen", side_effect=AssertionError("Offline benchmark forbids network")), \
         patch.object(ScopedSkill, "run", observe), patch.object(AnswerAgent, "compose", compose):
        try:
            if backend == "workflow":
                agent = CoordinatorAgent(scope=scope, db_path=evidence_path, use_llm_routing=False, enable_conversation_memory=False)
                result["answer"] = agent.ask(case.question)
                # Parse the actual displayed citation syntax, not all retrieved evidence.
                pairs = re.findall(r"evidence_id=(ev_[0-9a-f]{64}) hash=([0-9a-f]{64})", result["answer"])
                result["citation_refs"] = [{"evidence_id": identifier, "content_hash": digest} for identifier, digest in pairs]
                result["decision_memories"] = captured_memories
                result["termination_reason"] = "not_reported_by_workflow"
            elif backend == "react":
                agent = ReActAgent(scope=scope, db_path=evidence_path)
                output = agent.run_result(case.question)
                result.update({"run_id": output.run_id, "answer": output.answer, "raw_investigation": output.model_dump(mode="json"),
                    "mode": output.mode, "termination_reason": output.termination_reason,
                    "citation_refs": [r.model_dump() for r in output.evidence_refs], "decision_memories": output.decision_memories,
                    "invalid_actions": sum(e.error_code == "protocol_error" for s in output.trace for e in s.errors),
                    "duplicate_calls": sum(e.error_code == "duplicate_call" for s in output.trace for e in s.errors)})
            else:
                raise ValueError("Unknown backend")
        except Exception as exc:
            result["runtime_error"] = type(exc).__name__  # no raw environment/credentials
    result["latency_ms"] = (time.perf_counter() - start) * 1000
    result["events"] = events
    result["tool_attempts"] = len(events)
    # A single global order means first observed result; parallel completion is nondeterministic.
    result["retrieved_ids"] = [identifier for event in events for identifier in event["evidence_ids"]]
    if backend == "workflow":
        # Workflow has a final rank; report that order before other observed candidates.
        result["retrieved_ids"] = [h["evidence_id"] for h in captures] + [i for e in sorted(events, key=lambda e: e["tool_name"]) for i in e["evidence_ids"]]
    elif backend == "react" and "raw_investigation" in result:
        # Skill candidates can exceed ToolExecutor's bound. Score only delivered IDs.
        result["retrieved_ids"] = [i for s in result["raw_investigation"]["trace"] if s["action"] == "tool_attempt" for i in s["evidence_ids"]]
    result["failure_triggered"] = any(e["error"] == "injected_timeout" for e in events)
    store = EvidenceStore(evidence_path)
    result["valid_cited_ids"] = [r["evidence_id"] for r in result["citation_refs"] if store.get(r["evidence_id"], scope, r["content_hash"]) is not None]
    result["valid_citations"], result["citation_count"] = len(result["valid_cited_ids"]), len(result["citation_refs"])
    return result


def score(raw, case, label, manual_grade=None):
    """Evaluator-only join. Semantic correctness is never inferred from strings/reason."""
    row = {**raw, "case_id": case["case_id"], "category": case["category"], "gold_evidence_ids": label["gold_evidence_ids"],
           "status": "ungraded", "human_task_success": None, "human_appropriate_abstention": None,
           "decision_checks": None, "operational_recovery": False, "unsupported_reasons": []}
    if label.get("required_capabilities"):
        row["status"] = "unsupported"
        row["unsupported_reasons"] = list(label["required_capabilities"])
    # Both executed modes here lack model feedback. Current Decision Memory is available to BOTH.
    if manual_grade and row["status"] != "unsupported":
        if not manual_grade.get("reviewer") or type(manual_grade.get("task_success")) is not bool:
            raise ValueError("Manual grade requires a reviewer and explicit boolean task_success")
        if manual_grade.get("appropriate_abstention") is not None and type(manual_grade["appropriate_abstention"]) is not bool:
            raise ValueError("Abstention grade must be an explicit boolean or null")
        row.update(status="graded", human_task_success=manual_grade["task_success"],
                   human_appropriate_abstention=manual_grade.get("appropriate_abstention"),
                   human_review={k: manual_grade[k] for k in ("reviewer", "notes") if k in manual_grade})
    failures = [e["order"] for e in raw["events"] if e["error"]]
    if failures:
        row["operational_recovery"] = any(e["order"] > min(failures) and not e["error"] and
            bool(set(e["evidence_ids"]) & set(label["gold_evidence_ids"])) for e in raw["events"])
    if "decision_id" in label:
        memories = [m for m in raw["decision_memories"] if m["decision_id"] == label["decision_id"]]
        current = [m["version_id"] for m in memories if m["system_current"]]
        checks = {}
        if label["expected_current_version_id"] is not None:
            checks["current_version"] = current == [label["expected_current_version_id"]]
        if label.get("gold_decision_version_ids"):
            checks["historical_versions"] = set(label["gold_decision_version_ids"]).issubset({m["version_id"] for m in memories})
        if label.get("expected_supersedes"):
            checks["supersedes"] = all(any(m["version_id"] == vid and m["supersedes_version_id"] == previous for m in memories)
                                      for vid, previous in label["expected_supersedes"].items())
        if label.get("no_confirmed_version"):
            memory_query = any(e["tool_name"] == "search_memory" and not e["error"] for e in raw["events"])
            checks["pending_not_confirmed"] = memory_query and not memories and bool(set(raw["retrieved_ids"]) & set(label["gold_evidence_ids"]))
        if memories: checks["world_unknown"] = all(m["world_state"] == "unknown" for m in memories)
        row["decision_checks"] = checks
    return row


def configuration(data):
    sources = ["src/graph.py", "src/agents/react_agent.py", "src/agents/coordinator_agent.py", "src/agents/answer_agent.py",
               "src/retrieval.py", "src/skills/registry.py", "src/investigation.py", "src/memory/decisions.py", "src/llm_smoke.py",
               "src/investigation_context.py", "benchmark/runner.py", "benchmark/metrics.py", "benchmark/dataset.py", "benchmark/scripts/build.py", "benchmark/scripts/grade.py"]
    project = ROOT.parent
    return {"snapshot_id": data["manifest"]["snapshot_id"], "manifest_sha256": hashlib.sha256((data["root"] / "manifest.json").read_bytes()).hexdigest(),
        "execution_mode": "offline_no_model", "online_status": "pending_authorization", "model": None, "temperature": None,
        "react_prompt_sha256": hashlib.sha256(REACT_SYSTEM_PROMPT.encode()).hexdigest(),
        "source_sha256": {s: hashlib.sha256((project / s).read_bytes()).hexdigest() for s in sources},
        "retrieval": "existing scoped BM25, no FAISS", "skill_top_k": 5,
        "workflow": {"routing": "keyword", "max_skills": "existing route selection", "model_budget": "not implemented by Workflow"},
        "react": {"mode": "offline_fallback", "max_model_rounds": 5, "max_tool_calls": 8, "max_tokens": 64000,
                  "max_output_tokens": 2048, "max_context_bytes": 48000, "max_time_relaxations": 1},
        "limitations": ["No online/model quality comparison", "Pending independent human labels", "Semantic scores ungraded", "Different control-flow budgets disclosed"],
        "packages": {p: importlib.metadata.version(p) for p in ("langgraph", "pydantic")}}


def run(root=ROOT, output=None, split="test"):
    if split not in ("test", "dev"): raise ValueError("Select test or dev explicitly")
    data = load(root)
    output = Path(output) if output else ROOT / "reports" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    config = configuration(data)
    rows = []
    with tempfile.TemporaryDirectory(prefix="cma-benchmark-") as temporary:
        validation = validate(data, Path(temporary) / "validation")
        for case in data["cases"]:
            if case["split"] != split: continue
            label = data["labels"][case["case_id"]]
            for backend in ("workflow", "react"):
                runtime = materialize(data, Path(temporary) / (case["case_id"] + "-" + backend))
                raw = run_backend(case, runtime, backend, label.get("injected_failure"))
                rows.append(score(raw, case, label))
    summary = {"config": config, "validation": validation, "split": split,
               "backends": {b: aggregate([r for r in rows if r["backend"] == b]) for b in ("workflow", "react")}}
    (output / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output, summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--split", choices=["test", "dev"], default="test")
    args = parser.parse_args()
    path, summary = run(args.root, args.output, args.split)
    print(json.dumps({"report": str(path), **summary}, ensure_ascii=False, indent=2))
