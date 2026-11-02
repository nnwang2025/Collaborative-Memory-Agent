"""Independent task-level labels, deterministic signals and manual-only success.

The agent runtime never imports this module.  Public questions are handed to the
runtime as plain text; hidden labels are joined only after a run.  Deterministic
signals (fact markers, evidence recall, citation identity, tool recovery) are
deliberately reported separately from human task success.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from benchmark.dataset import ROOT as BENCHMARK_ROOT, load as load_dataset, materialize
from src.evidence import EvidenceScope
from src.memory.decisions import DecisionStore

TASKS_DIR = Path(__file__).resolve().parent
TASK_SET_VERSION = "cma-task-eval-v1"
# Authority is explicit so two different label systems are never silently mixed.
TASK_LEVEL_AUTHORITY = "benchmark/tasks/labels.json"
RETRIEVAL_GOLD_AUTHORITY = "benchmark/labels/gold.json"
SCORING_METHOD_VERSION = "cma-task-eval-method-v2"
TASK_CATEGORIES = (
    "single_source", "cross_source", "multi_round", "decision_history",
    "reviewed_vs_discussion", "conflict", "insufficient", "tool_failure",
)
SOURCE_TYPES = {"git_commit", "issue", "document", "meeting", "chat", "log", "code", "memory", "unknown"}
AUTHORITY_LEVELS = {"discussion", "meeting_record", "reviewed_decision", "execution", "source_evidence"}
CAPABILITIES = {"time_filters", "history_view", "feedback_loop", "failure_recovery"}
REASONS = {"sufficient", "insufficient", "conflict", "budget", "tool_failure"}
SKILLS = {"memory", "problem", "document", "dependency", "entrypoint"}
OFFLINE_UNSUPPORTED = set(CAPABILITIES)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Task(StrictModel):
    task_id: str = Field(min_length=1, max_length=80)
    case_id: str = Field(min_length=1, max_length=80)
    category: Literal["single_source", "cross_source", "multi_round", "decision_history",
                      "reviewed_vs_discussion", "conflict", "insufficient", "tool_failure"]
    project_id: str
    dataset_kind: Literal["live", "fixture"]
    split: Literal["dev", "test"]
    question: str = Field(min_length=1, max_length=2000)


class TaskSet(StrictModel):
    task_set_version: str
    corpus_snapshot_id: str
    tasks: list[Task]


class KeyFact(StrictModel):
    fact_id: str
    statement: str
    required: bool = True
    marker_groups: list[Annotated[list[str], Field(min_length=1)]] = Field(min_length=1)


class AcceptableEvidence(StrictModel):
    evidence_id: str = Field(pattern=r"^ev_[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExpectedAuthority(StrictModel):
    levels: list[Literal["discussion", "meeting_record", "reviewed_decision", "execution", "source_evidence"]]
    notes: str


class ExpectedTemporal(StrictModel):
    known_at: str | None = None
    expected_current_version_id: str | None = None
    expected_version_ids: list[str] = Field(default_factory=list)
    expected_supersedes: dict[str, str] = Field(default_factory=dict)
    requires_history: bool = False
    unknown_time_policy: str

    @field_validator("known_at")
    @classmethod
    def timezone_required(cls, value):
        if value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError("known_at requires an explicit timezone")
        return value


class ExpectedConflict(StrictModel):
    field: str
    values: list[str] = Field(min_length=2)
    must_disclose_both: bool = True


class Abstention(StrictModel):
    required: bool
    expected_termination: list[Literal["sufficient", "insufficient", "conflict", "budget", "tool_failure"]]
    must_disclose: list[Annotated[list[str], Field(min_length=1)]] = Field(default_factory=list)


class InjectedFailure(StrictModel):
    skill: Literal["memory", "problem", "document", "dependency", "entrypoint"]
    error: str


class TaskLabel(StrictModel):
    task_id: str
    category: Literal["single_source", "cross_source", "multi_round", "decision_history",
                      "reviewed_vs_discussion", "conflict", "insufficient", "tool_failure"]
    decision_id: str | None = Field(default=None, pattern=r"^dec_[0-9a-f]{32}$")
    key_facts: list[KeyFact] = Field(min_length=1)
    acceptable_evidence: list[AcceptableEvidence] = Field(min_length=1)
    required_sources: list[str] = Field(default_factory=list)
    expected_authority: ExpectedAuthority
    expected_temporal: ExpectedTemporal
    expected_conflict: ExpectedConflict | None = None
    abstention: Abstention
    required_capabilities: list[Literal["time_filters", "history_view", "feedback_loop", "failure_recovery"]] = Field(default_factory=list)
    injected_failure: InjectedFailure | None = None
    human_review_rules: list[str] = Field(min_length=1)
    missing_fact: str | None = None
    no_confirmed_version: bool = False

    @model_validator(mode="after")
    def coherent_rules(self):
        for fact in self.key_facts:
            if any(not alternative.strip() for group in fact.marker_groups for alternative in group):
                raise ValueError("Blank fact marker")
        for group in self.abstention.must_disclose:
            if any(not alternative.strip() for alternative in group):
                raise ValueError("Blank abstention marker")
        if self.no_confirmed_version and not self.abstention.required:
            raise ValueError("A no-confirmed-version task must require abstention")
        if self.expected_temporal.requires_history and len(self.expected_temporal.expected_version_ids) < 2:
            raise ValueError("A history task must name at least two expected versions")
        if set(self.required_sources) - SOURCE_TYPES:
            raise ValueError("Unknown required source type")
        return self


class LabelFile(StrictModel):
    task_set_version: str
    corpus_snapshot_id: str
    labels: dict[str, TaskLabel]


def load_tasks(directory=TASKS_DIR):
    directory = Path(directory).resolve()
    public_path, labels_path = directory / "public.json", directory / "labels.json"
    public = TaskSet.model_validate_json(public_path.read_text(encoding="utf-8"))
    labels = LabelFile.model_validate_json(labels_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(public_path.read_bytes() + b"\0" + labels_path.read_bytes()).hexdigest()
    return {"directory": directory, "public": public, "labels": labels, "task_set_sha256": digest,
            "by_id": {task.task_id: task for task in public.tasks}}


def _task_set_sha256(tasks):
    return tasks.get("task_set_sha256") or hashlib.sha256(
        (tasks["directory"] / "public.json").read_bytes() + b"\0" +
        (tasks["directory"] / "labels.json").read_bytes()).hexdigest()


def validate_tasks(tasks=None, dataset=None, directory=None):
    tasks = tasks or load_tasks()
    dataset = dataset or load_dataset()
    if directory is None:
        with tempfile.TemporaryDirectory(prefix="cma-task-validation-") as temporary:
            return _validate(tasks, dataset, Path(temporary))
    return _validate(tasks, dataset, Path(directory))


def _validate(tasks, dataset, directory):
    public, labels = tasks["public"], tasks["labels"]
    if public.task_set_version != TASK_SET_VERSION or labels.task_set_version != TASK_SET_VERSION:
        raise ValueError("Task set version mismatch")
    if public.task_set_version != labels.task_set_version:
        raise ValueError("Public task/label version mismatch")
    snapshot = dataset["manifest"]["snapshot_id"]
    if public.corpus_snapshot_id != snapshot or labels.corpus_snapshot_id != snapshot:
        raise ValueError("Task set corpus snapshot mismatch")
    ids = [task.task_id for task in public.tasks]
    if len(ids) != len(set(ids)) or set(ids) != set(labels.labels):
        raise ValueError("Task/label ID mismatch")
    if len(ids) != 16:
        raise ValueError("Task set must contain exactly 16 tasks")
    counts = {category: 0 for category in TASK_CATEGORIES}
    records = {record.evidence_id: record for record in dataset["records"]}
    scopes = {(record.project_id, record.dataset_kind) for record in dataset["records"]}
    evidence_path, memory_path = materialize(dataset, directory)
    memory = DecisionStore(memory_path, evidence_db_path=evidence_path)
    for task in public.tasks:
        label = labels.labels[task.task_id]
        if task.task_id != label.task_id or task.category != label.category:
            raise ValueError("Task/label identity or category mismatch: " + task.task_id)
        if (task.project_id, task.dataset_kind) not in scopes:
            raise ValueError("Task scope not present in corpus: " + task.task_id)
        counts[task.category] += 1
        if not label.human_review_rules or any(not rule.strip() for rule in label.human_review_rules):
            raise ValueError("Task review rules are empty: " + task.task_id)
        if not any(fact.required for fact in label.key_facts):
            raise ValueError("Task has no required key fact: " + task.task_id)
        for item in label.acceptable_evidence:
            record = records.get(item.evidence_id)
            if record is None:
                raise ValueError("Acceptable evidence missing: " + item.evidence_id)
            if record.content_hash != item.content_hash:
                raise ValueError("Acceptable evidence hash mismatch: " + item.evidence_id)
            if (record.project_id, record.dataset_kind) != (task.project_id, task.dataset_kind):
                raise ValueError("Acceptable evidence outside task scope: " + item.evidence_id)
        if label.expected_conflict:
            combined = "\n".join(records[item.evidence_id].content for item in label.acceptable_evidence)
            if any(value not in combined for value in label.expected_conflict.values):
                raise ValueError("Conflict value missing from acceptable evidence: " + task.task_id)
        if label.decision_id:
            scope = EvidenceScope(project_id=task.project_id, dataset_kind=task.dataset_kind)
            state = memory.snapshot(label.decision_id, scope, known_at=label.expected_temporal.known_at)
            versions = {version["version_id"]: version for version in state["versions"]}
            if not set(label.expected_temporal.expected_version_ids).issubset(versions):
                raise ValueError("Expected decision version missing: " + task.task_id)
            current = (state["current_confirmed"] or {}).get("version_id")
            expected_current = label.expected_temporal.expected_current_version_id
            if current != expected_current:
                raise ValueError("Expected current decision version mismatch: " + task.task_id)
            if label.no_confirmed_version:
                if current is not None or not any(p["review_status"] == "pending" for p in state["proposals"]):
                    raise ValueError("No-confirmed-version label is inconsistent: " + task.task_id)
            for version, previous in label.expected_temporal.expected_supersedes.items():
                if version not in versions or versions[version]["supersedes_version_id"] != previous:
                    raise ValueError("Expected supersedes mismatch: " + task.task_id)
        if label.abstention.required and "insufficient" not in label.abstention.expected_termination:
            raise ValueError("Required abstention must accept insufficient: " + task.task_id)
    if any(count != 2 for count in counts.values()):
        raise ValueError("Task set needs exactly two tasks per category")
    return {
        "task_set_version": public.task_set_version,
        "task_set_sha256": _task_set_sha256(tasks),
        "corpus_snapshot_id": snapshot,
        "task_count": len(ids),
        "category_counts": counts,
        "evidence_records": len(records),
        "task_set_status": "structurally_valid_pending_human_review",
        "scoring_isolation": "labels_joined_after_run_only",
        "offline_capability_policy": "time_filters/history_view/feedback_loop/failure_recovery are unverified under offline_fallback",
    }


def _normalise(text):
    text = text or ""
    text = re.sub(r"ev_[0-9a-f]{64}", " ", text)
    text = re.sub(r"\b[0-9a-f]{64}\b", " ", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{20,}\b", " ", text)
    text = re.sub(r"https?://\S+|benchmark://\S+", " ", text)
    text = text.lower().replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def _marker_found(text, marker):
    normal_text, normal_marker = _normalise(text), _normalise(marker)
    if not normal_marker:
        return False
    if re.fullmatch(r"[a-z0-9]{1,2}", normal_marker):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(normal_marker) + r"(?![a-z0-9])", normal_text))
    if normal_marker in normal_text:
        return True
    if re.search(r"[一-鿿]", normal_marker):
        return normal_marker.replace(" ", "") in normal_text.replace(" ", "")
    return False


def _group_matches(text, groups):
    found = [_marker_found(text, group[0]) if len(group) == 1 else any(_marker_found(text, alternative) for alternative in group)
             for group in groups]
    return found


def _distinct(values):
    return list(dict.fromkeys(values))


def _rate(numerator, denominator):
    return numerator / denominator if denominator else None


def _memories(raw, label):
    return [m for m in raw.get("decision_memories", []) if m.get("decision_id") == label.decision_id]


def _steps(raw):
    return (raw.get("raw_investigation") or {}).get("trace") or []


def _claims(raw):
    return (raw.get("raw_investigation") or {}).get("claims") or []


def _conflicts(raw):
    return (raw.get("raw_investigation") or {}).get("conflicts") or []


def _claim_counts(raw):
    return (raw.get("raw_investigation") or {}).get("claim_counts") or {}


_ANSWER_HEADER = re.compile(r"^【([^】]+)】")
# Pending claims are explicitly excluded: they are not confirmed answer facts.
_VERIFIED_CLAIM_SECTION_MARKERS = ("有引用支持的原文摘录", "已保留的结论", "回答要点")


def _section_bullets(lines):
    bullets = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        content = stripped[1:].strip()
        content = re.sub(r"\s*\[[^\]]*\]\s*$", "", content)
        if content:
            bullets.append(content)
    return bullets


_UNCONFIRMED_SECTION = re.compile(r"^未确认部分\s*[:：]\s*")
_UNCONFIRMED_TRAILER = re.compile(r"\s*（完整见【未解决的缺口】[^）]*）\s*。?\s*$")


def _synthesis_body_lines(lines):
    """Answer-bearing synthesis lines: bullets plus the unconfirmed content.

    System-generated section labels (for example ``未确认部分：``) are stripped
    before fact matching: a heading must never satisfy a fact marker by itself.
    The unconfirmed content after the label is retained so real conclusions can
    still be scored.  Question echo, empty-result notices and the ``说明`` caveat
    remain excluded.
    """
    body = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("-"):
            content = re.sub(r"\s*\[[^\]]*\]\s*$", "", stripped[1:].strip()).strip()
            if content:
                body.append(content)
        elif stripped.startswith("未确认部分"):
            content = _UNCONFIRMED_SECTION.sub("", stripped, count=1)
            content = _UNCONFIRMED_TRAILER.sub("", content).strip()
            if content:
                body.append(content)
    return body


def _answer_body(answer):
    """Return only confirmed answer claims, never evidence/gap/conflict bulk text.

    The synthesis section is the primary user answer.  When it is absent, only
    verified claim sections count as answer body; pending/conflicted extracts are
    deliberately excluded.  Older rendered answers without a claim section are
    explicitly unverified for fact coverage.  A plain answer with no section
    headers is treated as the answer body because no section boundary exists to
    confuse the extractor.
    """
    text = answer or ""
    if not _ANSWER_HEADER.search(text):
        return text, "plain_text"
    sections, current = {}, None
    for line in text.splitlines():
        header = _ANSWER_HEADER.match(line.strip())
        if header:
            current = header.group(1)
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)
    synthesis_lines = []
    for header, lines in sections.items():
        if header.startswith("综合回答"):
            synthesis_lines.extend(_synthesis_body_lines(lines))
    if synthesis_lines:
        return "\n".join(synthesis_lines), "rendered_synthesis"
    claim_lines = []
    for header, lines in sections.items():
        if not any(marker in header for marker in _VERIFIED_CLAIM_SECTION_MARKERS):
            continue
        for content in _section_bullets(lines):
            content = re.sub(r"^(?:历史来源记载|原文摘录)[：:]\s*", "", content)
            if content:
                claim_lines.append(content)
    if claim_lines:
        return "\n".join(claim_lines), "rendered_claim_section"
    return "", "unverified_no_answer_body"


def _successful_memory_queries(raw):
    queries = []
    for event in raw.get("events", []):
        if event.get("tool_name") == "search_memory" and not event.get("error"):
            # Keep structured filters structured: stringifying a dict hides
            # keys such as known_at from the per-query time checks.
            queries.append({"filters": event.get("filters") or {}})
    for step in _steps(raw):
        if step.get("action") == "tool_attempt" and step.get("tool_name") == "search_memory" and not step.get("errors"):
            queries.append({"filters": step.get("filters") or {}})
    return queries


def _filter_value(query, key):
    filters = query.get("filters")
    if isinstance(filters, dict):
        return filters.get(key)
    match = re.search(r"\b" + re.escape(key) + r"=(None|'[^']*'|\"[^\"]*\"|[^ ]+)", str(filters))
    if not match:
        return None
    value = match.group(1)
    return None if value == "None" else value.strip("'\"") or None


def _known_at_applied(raw, label):
    expected = label.expected_temporal.known_at
    if not expected:
        return None
    for query in _successful_memory_queries(raw):
        observed = _filter_value(query, "known_at")
        if observed and observed[:10] == expected[:10]:
            return True
    return False


def _history_applied(raw, label):
    # Explicit label intent, not inferred from version counts: supersedes-only
    # labels must not be reported as missing a history query they never needed.
    if not label.expected_temporal.requires_history:
        return None
    return any(_filter_value(query, "memory_view") == "history" for query in _successful_memory_queries(raw))


def _memories_for_snapshot(raw, label):
    """Scope memories to the query whose knowledge_cutoff matches the label.

    Records from a current view and a historical cutoff view are different
    snapshots: they must not be merged when checking one point in time.  If no
    cutoff-tagged record matches, fall back to all records so a mismatch stays
    visible as a failed check rather than being hidden.
    """
    memories = _memories(raw, label)
    cutoff = label.expected_temporal.known_at
    if not cutoff:
        return memories
    dated = [memory for memory in memories
             if str(memory.get("knowledge_cutoff") or "")[:10] == cutoff[:10]]
    return dated or memories


def _decision_checks(raw, label):
    if not label.decision_id:
        return None
    memories = _memories_for_snapshot(raw, label)
    versions = {memory["version_id"] for memory in memories}
    current = [memory["version_id"] for memory in memories if memory.get("system_current")]
    checks = {}
    expected_current = label.expected_temporal.expected_current_version_id
    if expected_current is not None:
        checks["current_version_correct"] = current == [expected_current]
    elif label.no_confirmed_version:
        checks["current_version_correct"] = not current
    if label.expected_temporal.expected_version_ids:
        checks["historical_versions_present"] = set(label.expected_temporal.expected_version_ids).issubset(versions)
    if label.expected_temporal.expected_supersedes:
        checks["supersedes_correct"] = all(
            any(memory["version_id"] == version and memory.get("supersedes_version_id") == previous for memory in memories)
            for version, previous in label.expected_temporal.expected_supersedes.items())
    if label.no_confirmed_version:
        checks["pending_not_confirmed"] = bool(_successful_memory_queries(raw)) and not memories
    checks["known_at_applied"] = _known_at_applied(raw, label)
    checks["history_view_applied"] = _history_applied(raw, label)
    checks["world_state_unknown"] = bool(memories) and all(memory.get("world_state") == "unknown" for memory in memories)
    if label.expected_temporal.known_at:
        checks["snapshot_respects_cutoff"] = bool(memories) and all(
            (memory.get("decision_known_at") or "")[:10] <= label.expected_temporal.known_at[:10]
            for memory in memories)
    return checks


def _authority_checks(raw, label, records):
    authorities = {memory.get("authority") for memory in _memories(raw, label)}
    observed_sources = {records[i].source_type for i in raw.get("retrieved_ids", []) if i in records}
    levels = set()
    if "chat" in observed_sources:
        levels.add("discussion")
    if "meeting" in observed_sources:
        levels.add("meeting_record")
    if authorities & {"reviewed_local_decision", "reviewed_decision"}:
        levels.add("reviewed_decision")
    if any(records[i].source_type in {"document", "issue", "log", "git_commit", "code", "chat", "meeting"}
           for i in raw.get("retrieved_ids", []) if i in records):
        levels.add("source_evidence")
    return {
        "observed_levels": sorted(levels),
        "discussion_observed": "discussion" in levels,
        "reviewed_decision_observed": "reviewed_decision" in levels,
        "execution_observed": "execution" in levels,
        "expected_levels": list(label.expected_authority.levels),
        "reviewed_vs_discussion_disclosed": ("discussion" in levels and "reviewed_decision" in levels)
            if label.category == "reviewed_vs_discussion" else None,
    }


def _fact_score(answer, label):
    body, basis = _answer_body(answer)
    required = [fact for fact in label.key_facts if fact.required]
    details = []
    for fact in required:
        matches = _group_matches(body, fact.marker_groups)
        details.append({"fact_id": fact.fact_id, "matched": all(matches),
                        "groups_matched": sum(matches), "group_count": len(matches)})
    matched = sum(item["matched"] for item in details)
    evaluable = basis != "unverified_no_answer_body"
    return {"required_fact_count": len(required), "matched_fact_count": matched,
            "task_fact_coverage": _rate(matched, len(required)) if evaluable else None,
            "task_fact_coverage_status": basis,
            "answer_body_chars": len(body),
            "facts": details}


def _evidence_score(raw, label, records, answer):
    acceptable = [item.evidence_id for item in label.acceptable_evidence]
    retrieved = _distinct(raw.get("retrieved_ids", []))
    retrieved_acceptable = [i for i in acceptable if i in retrieved]
    cited = raw.get("citation_refs", [])
    cited_ids = _distinct(item["evidence_id"] for item in cited)
    cited_acceptable = [i for i in acceptable if i in cited_ids]
    required_sources = set(label.required_sources)
    observed_sources = {records[i].source_type for i in retrieved if i in records}
    claim_refs = [ref for claim in _claims(raw) for ref in claim.get("refs", [])]
    valid_claim_refs = [ref for ref in claim_refs if ref.get("evidence_id") in records
                        and records[ref["evidence_id"]].content_hash == ref.get("content_hash")]
    return {
        "acceptable_evidence_count": len(acceptable),
        "retrieved_acceptable_count": len(retrieved_acceptable),
        "evidence_recall": _rate(len(retrieved_acceptable), len(acceptable)),
        "citation_count": len(cited),
        "valid_citation_count": raw.get("valid_citations", 0),
        "citation_validity_rate": raw.get("citation_validity_rate",
            _rate(raw.get("valid_citations", 0), len(cited))),
        "cited_acceptable_count": len(cited_acceptable),
        "cited_acceptable_evidence_coverage": _rate(len(cited_acceptable), len(acceptable)),
        "claim_reference_count": len(claim_refs),
        "valid_claim_reference_count": len(valid_claim_refs),
        "claim_reference_validity_rate": _rate(len(valid_claim_refs), len(claim_refs)),
        "required_sources": sorted(required_sources),
        "observed_sources": sorted(observed_sources),
        "source_recall": _rate(len(required_sources & observed_sources), len(required_sources)),
        "answer_has_citation": bool(cited),
        "answer_chars": len(answer or ""),
    }


def _conflict_score(answer, raw, label):
    if not label.expected_conflict:
        return None
    values = label.expected_conflict.values
    found = [_marker_found(answer, value) for value in values]
    return {
        "expected_field": label.expected_conflict.field,
        "values_found": found,
        "both_values_disclosed": all(found),
        "conflict_recorded": bool(_conflicts(raw)),
        "termination_conflict_or_insufficient": raw.get("termination_reason") in {"conflict", "insufficient"},
        "deterministic_disclosure_signal": all(found) and (bool(_conflicts(raw)) or
                                                            raw.get("termination_reason") in {"conflict", "insufficient"}),
    }


def _abstention_score(answer, raw, label):
    coverage = _group_matches(answer, label.abstention.must_disclose) if label.abstention.must_disclose else []
    expected_met = raw.get("termination_reason") in label.abstention.expected_termination
    return {
        "required": label.abstention.required,
        "expected_termination": list(label.abstention.expected_termination),
        "observed_termination": raw.get("termination_reason"),
        "expected_termination_met": expected_met,
        "must_disclose_group_count": len(coverage),
        "must_disclose_groups_matched": sum(coverage),
        "must_disclose_coverage": _rate(sum(coverage), len(coverage)),
        "deterministic_abstention_signal": expected_met and all(coverage) if label.abstention.required else None,
    }


def _recovery_score(answer, raw, label):
    if not label.injected_failure:
        return None
    events = sorted(raw.get("events", []), key=lambda event: event.get("order", 0))
    failures = [event["order"] for event in events if event.get("error")]
    acceptable = {item.evidence_id for item in label.acceptable_evidence}
    recovered = bool(failures) and any(
        event.get("order", 0) > min(failures) and not event.get("error")
        and bool(set(event.get("evidence_ids", [])) & acceptable) for event in events)
    coverage = _group_matches(answer, label.abstention.must_disclose) if label.abstention.must_disclose else []
    return {
        "failure_injected": True,
        "failure_triggered": bool(failures),
        "post_failure_acceptable_evidence": recovered,
        "operational_recovery": recovered,
        "disclosure_coverage": _rate(sum(coverage), len(coverage)),
    }


def _protocol_score(raw):
    trace = _steps(raw)
    model_decisions = [step for step in trace if step.get("action") == "model_decision"]
    model_calls = raw.get("model_calls", len(model_decisions))
    protocol_errors = raw.get("invalid_actions", 0)
    model_errors = sum(any(error.get("error_code") == "model_error" for error in step.get("errors", []))
                       for step in model_decisions)
    return {
        "model_calls": model_calls,
        "protocol_error_count": protocol_errors,
        "protocol_validity_rate": _rate(model_calls - protocol_errors, model_calls) if model_calls else None,
        "model_error_count": model_errors,
        "model_call_success_rate": _rate(model_calls - model_errors, model_calls) if model_calls else None,
    }


def _manual_grade(label, grade):
    if not grade:
        return {"status": "not_graded", "task_success": None, "appropriate_abstention": None,
                "tool_failure_recovery": None, "reviewer": None, "notes": None}
    if not grade.get("reviewer") or type(grade.get("task_success")) is not bool:
        raise ValueError("Manual grade requires a reviewer and explicit boolean task_success")
    abstention = grade.get("appropriate_abstention")
    if abstention is not None and type(abstention) is not bool:
        raise ValueError("appropriate_abstention must be boolean or null")
    recovery = grade.get("tool_failure_recovery")
    if recovery is not None and type(recovery) is not bool:
        raise ValueError("tool_failure_recovery must be boolean or null")
    return {
        "status": "graded",
        "task_success": grade["task_success"],
        "appropriate_abstention": abstention if label.abstention.required else None,
        "tool_failure_recovery": recovery if label.injected_failure else None,
        "reviewer": grade["reviewer"],
        "notes": grade.get("notes", ""),
    }


def score_task(raw, task, label, records, *, execution_mode, supported_capabilities=None, manual_grade=None):
    """Join labels only after execution and keep every metric separately visible."""
    answer = raw.get("answer", "")
    capability_status = {capability: "unverified_offline_adapter"
                         for capability in label.required_capabilities}
    if supported_capabilities is not None:
        for capability in label.required_capabilities:
            if capability in supported_capabilities:
                capability_status[capability] = "supported"
    unsupported = [capability for capability in label.required_capabilities
                   if capability_status[capability] == "unverified_offline_adapter"]
    manual = _manual_grade(label, manual_grade)
    events = raw.get("events", [])
    tool_failures = sum(bool(event.get("error")) for event in events)
    tool_attempts = raw.get("tool_attempts", len(events))
    evidence = _evidence_score(raw, label, records, answer)
    facts = _fact_score(answer, label)
    decision_checks = _decision_checks(raw, label)
    conflict = _conflict_score(answer, raw, label)
    abstention = _abstention_score(answer, raw, label)
    recovery = _recovery_score(answer, raw, label)
    protocol = _protocol_score(raw)
    runtime_error = raw.get("runtime_error")
    if runtime_error:
        status = "runtime_error"
    elif raw.get("mode") == "offline_fallback" and execution_mode == "online_model":
        status = "offline_fallback_not_real_model"
    elif unsupported:
        status = "unsupported_offline" if execution_mode == "offline_no_model" else "unsupported"
    elif manual["status"] == "graded":
        status = "graded"
    elif execution_mode == "offline_no_model":
        status = "ungraded_offline_adapter"
    else:
        status = "pending_human_review"
    if manual["status"] == "graded" and status != "graded":
        manual = {**manual, "status": "rejected_unsupported", "task_success": None,
                  "appropriate_abstention": None, "tool_failure_recovery": None}
    row = {
        "task_id": task.task_id,
        "case_id": task.case_id,
        "category": task.category,
        "project_id": task.project_id,
        "question": task.question,
        "backend": raw.get("backend"),
        "execution_mode": execution_mode,
        "status": status,
        "mode": raw.get("mode"),
        "runtime_error": runtime_error,
        "termination_reason": raw.get("termination_reason"),
        "termination_detail": raw.get("termination_detail"),
        "answer": answer,
        "events": events,
        "decision_memories": raw.get("decision_memories", []),
        "trace": _steps(raw),
        "claims": _claims(raw),
        "claim_counts": _claim_counts(raw),
        "conflicts": _conflicts(raw),
        "task_success": manual["task_success"],
        "semantic_correctness": "graded" if manual["status"] == "graded" else "unverified",
        "human_appropriate_abstention": manual["appropriate_abstention"],
        "human_tool_failure_recovery": manual["tool_failure_recovery"],
        "human_review": {k: manual[k] for k in ("reviewer", "notes") if manual.get(k)} or None,
        "capability_status": capability_status,
        "unsupported_capabilities": unsupported,
        "metrics": {
            **protocol,
            "tool_attempts": tool_attempts,
            "tool_failures": tool_failures,
            "tool_call_success_rate": _rate(tool_attempts - tool_failures, tool_attempts),
            "duplicate_calls": raw.get("duplicate_calls", 0),
            "latency_ms": raw.get("latency_ms"),
            "provider_input_tokens": raw.get("provider_input_tokens", 0),
            "provider_output_tokens": raw.get("provider_output_tokens", 0),
            "estimated_tokens": raw.get("estimated_tokens", 0),
            "cost_status": "unavailable_no_pricing",
            "estimated_cost_usd": None,
            **evidence,
            **facts,
            "decision_checks": decision_checks,
            "authority_checks": _authority_checks(raw, label, records),
            "conflict_checks": conflict,
            "abstention_checks": abstention,
            "recovery_checks": recovery,
        },
    }
    return row


def _sum(rows, path, denominator_path=None):
    def value(row):
        current = row["metrics"]
        for key in path:
            current = (current or {}).get(key) if isinstance(current, dict) else None
        return current
    values = [value(row) for row in rows if value(row) is not None]
    return sum(values) if values else 0


def _aggregate_metrics(rows):
    graded = [row for row in rows if row["status"] == "graded"]
    abstention_rows = [row for row in rows if row["metrics"]["abstention_checks"]["required"]
                       and row["human_appropriate_abstention"] is not None]
    recovery_rows = [row for row in rows if row["metrics"]["recovery_checks"]]
    latencies = [row["metrics"]["latency_ms"] for row in rows if row["metrics"]["latency_ms"] is not None]
    model_calls = sum(row["metrics"]["model_calls"] for row in rows)
    protocol_errors = sum(row["metrics"]["protocol_error_count"] for row in rows)
    tool_attempts = sum(row["metrics"]["tool_attempts"] for row in rows)
    tool_failures = sum(row["metrics"]["tool_failures"] for row in rows)
    citations = sum(row["metrics"]["citation_count"] for row in rows)
    valid_citations = sum(row["metrics"]["valid_citation_count"] for row in rows)
    claim_refs = sum(row["metrics"]["claim_reference_count"] for row in rows)
    valid_claim_refs = sum(row["metrics"]["valid_claim_reference_count"] for row in rows)
    acceptable = sum(row["metrics"]["acceptable_evidence_count"] for row in rows)
    retrieved_acceptable = sum(row["metrics"]["retrieved_acceptable_count"] for row in rows)
    cited_acceptable = sum(row["metrics"]["cited_acceptable_count"] for row in rows)
    required_facts = sum(row["metrics"]["required_fact_count"] for row in rows
                         if row["metrics"].get("task_fact_coverage_status") != "unverified_no_answer_body")
    matched_facts = sum(row["metrics"]["matched_fact_count"] for row in rows
                        if row["metrics"].get("task_fact_coverage_status") != "unverified_no_answer_body")
    fact_unverified = sum(row["metrics"].get("task_fact_coverage_status") == "unverified_no_answer_body"
                          for row in rows)
    required_sources = sum(len(row["metrics"]["required_sources"]) for row in rows)
    found_sources = sum(len(set(row["metrics"]["required_sources"]) & set(row["metrics"]["observed_sources"]))
                        for row in rows)
    return {
        "TaskSuccessRate": {"numerator": sum(row["task_success"] is True for row in graded),
                            "denominator": len(graded), "value": _rate(sum(row["task_success"] is True for row in graded), len(graded))},
        "AppropriateAbstentionRate": {"numerator": sum(row["human_appropriate_abstention"] is True for row in abstention_rows),
                                      "denominator": len(abstention_rows),
                                      "value": _rate(sum(row["human_appropriate_abstention"] is True for row in abstention_rows), len(abstention_rows))},
        "ProtocolValidityRate": {"numerator": model_calls - protocol_errors, "denominator": model_calls,
                                 "value": _rate(model_calls - protocol_errors, model_calls)},
        "ModelCallSuccessRate": {"numerator": model_calls - sum(row["metrics"]["model_error_count"] for row in rows),
                                 "denominator": model_calls,
                                 "value": _rate(model_calls - sum(row["metrics"]["model_error_count"] for row in rows), model_calls)},
        "ToolCallSuccessRate": {"numerator": tool_attempts - tool_failures, "denominator": tool_attempts,
                                "value": _rate(tool_attempts - tool_failures, tool_attempts)},
        "EvidenceRecall": {"numerator": retrieved_acceptable, "denominator": acceptable,
                           "value": _rate(retrieved_acceptable, acceptable)},
        "CitationValidityRate": {"numerator": valid_citations, "denominator": citations,
                                 "value": _rate(valid_citations, citations)},
        "CitedAcceptableEvidenceCoverage": {"numerator": cited_acceptable, "denominator": acceptable,
                                            "value": _rate(cited_acceptable, acceptable)},
        "ClaimReferenceValidityRate": {"numerator": valid_claim_refs, "denominator": claim_refs,
                                       "value": _rate(valid_claim_refs, claim_refs)},
        "TaskFactCoverage": {"numerator": matched_facts, "denominator": required_facts,
                             "value": _rate(matched_facts, required_facts)},
        "TaskFactCoverageUnverifiedRows": {"numerator": fact_unverified, "denominator": len(rows),
                                           "value": _rate(fact_unverified, len(rows))},
        "SourceRecall": {"numerator": found_sources, "denominator": required_sources,
                         "value": _rate(found_sources, required_sources)},
        "OperationalToolFailureRecoveryRate": {
            "numerator": sum(row["metrics"]["recovery_checks"]["operational_recovery"] for row in recovery_rows),
            "denominator": len(recovery_rows),
            "value": _rate(sum(row["metrics"]["recovery_checks"]["operational_recovery"] for row in recovery_rows), len(recovery_rows))},
        "HumanToolFailureRecoveryRate": {
            "numerator": sum(row["human_tool_failure_recovery"] is True for row in rows if row["human_tool_failure_recovery"] is not None),
            "denominator": sum(row["human_tool_failure_recovery"] is not None for row in rows),
            "value": _rate(sum(row["human_tool_failure_recovery"] is True for row in rows if row["human_tool_failure_recovery"] is not None),
                           sum(row["human_tool_failure_recovery"] is not None for row in rows))},
        "BudgetExhaustionRate": {"numerator": sum(row["termination_reason"] == "budget" for row in rows),
                                 "denominator": len(rows),
                                 "value": _rate(sum(row["termination_reason"] == "budget" for row in rows), len(rows))},
        "latency_mean_ms": sum(latencies) / len(latencies) if latencies else None,
        "latency_p95_ms": sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None,
        "model_calls": model_calls,
        "tool_attempts": tool_attempts,
        "provider_input_tokens": sum(row["metrics"]["provider_input_tokens"] for row in rows),
        "provider_output_tokens": sum(row["metrics"]["provider_output_tokens"] for row in rows),
        "estimated_tokens": sum(row["metrics"]["estimated_tokens"] for row in rows),
        "cost_status": "unavailable_no_pricing",
        "ClaimObservability": {
            "rows_with_claim_counts": sum(bool(row.get("claim_counts")) for row in rows),
            **{key: sum((row.get("claim_counts") or {}).get(key, 0) for row in rows)
               for key in ("claims_submitted", "claims_accepted",
                           "claims_discarded_by_validation", "claims_discarded_by_conflict",
                           "claims_retained_with_unresolved_conflict")},
        },
    }


def aggregate(rows):
    """Aggregate deterministic signals, but never turn them into task success."""
    result = {
        "overall": _aggregate_metrics(rows),
        "by_category": {category: _aggregate_metrics([row for row in rows if row["category"] == category])
                        for category in TASK_CATEGORIES},
        "by_backend": {backend: _aggregate_metrics([row for row in rows if row["backend"] == backend])
                       for backend in sorted({row["backend"] for row in rows})},
        "grading": {
            "total": len(rows),
            "graded": sum(row["status"] == "graded" for row in rows),
            "pending_human_review": sum(row["status"] == "pending_human_review" for row in rows),
            "unsupported_offline": sum(row["status"] == "unsupported_offline" for row in rows),
            "unsupported": sum(row["status"] == "unsupported" for row in rows),
            "ungraded_offline_adapter": sum(row["status"] == "ungraded_offline_adapter" for row in rows),
            "runtime_error": sum(row["status"] == "runtime_error" for row in rows),
            "offline_fallback_not_real_model": sum(row["status"] == "offline_fallback_not_real_model" for row in rows),
            "unverified_semantics": sum(row["status"] != "graded" for row in rows),
            "task_success_status": "manual_only_no_claim_without_human_grades",
            "success_guard": "Citations, evidence counts and deterministic fact markers do not constitute task success.",
        },
    }
    return result


def diagnose(row):
    """Return hypotheses, never an asserted root cause; labels are evaluator-only."""
    metrics = row["metrics"]
    checks = metrics["decision_checks"] or {}
    hypotheses = []
    if row["runtime_error"]:
        hypotheses.append("runtime_or_harness_error")
    if row["unsupported_capabilities"]:
        hypotheses.append("offline_adapter_limitation")
    if row["mode"] == "offline_fallback" and row["execution_mode"] == "online_model":
        hypotheses.append("online_authorization_or_credential_not_effective")
    if metrics["evidence_recall"] is not None and metrics["evidence_recall"] < 1:
        hypotheses.append("retrieval_recall_or_tool_selection_gap")
    if metrics["source_recall"] is not None and metrics["source_recall"] < 1:
        hypotheses.append("cross_source_selection_gap")
    if metrics.get("task_fact_coverage_status") == "unverified_no_answer_body":
        hypotheses.append("fact_coverage_unverified_no_answer_body")
    elif metrics["task_fact_coverage"] is not None and metrics["task_fact_coverage"] < 1:
        if metrics["evidence_recall"] == 1:
            hypotheses.append("answer_omission_or_marker_strictness")
        else:
            hypotheses.append("candidate_evidence_not_covered_by_answer")
    if checks.get("known_at_applied") is False:
        hypotheses.append("known_at_query_condition_not_applied")
    if checks.get("history_view_applied") is False:
        hypotheses.append("history_view_not_requested")
    if checks.get("snapshot_respects_cutoff") is False:
        hypotheses.append("time_snapshot_inconsistent")
    if metrics["conflict_checks"] and not metrics["conflict_checks"]["both_values_disclosed"]:
        hypotheses.append("conflict_disclosure_gap")
    if metrics["recovery_checks"] and not metrics["recovery_checks"]["operational_recovery"]:
        hypotheses.append("tool_failure_recovery_gap")
    if row["termination_reason"] == "budget":
        hypotheses.append("planning_or_budget_exhaustion")
    return {"hypotheses": sorted(set(hypotheses)), "deterministic_observations_only": True,
            "requires_online_or_human_confirmation": bool(hypotheses)}


def pending_grading(rows):
    items = []
    for row in rows:
        graded = row["status"] == "graded"
        items.append({
            "task_id": row["task_id"], "category": row["category"], "backend": row["backend"],
            "question": row.get("question"),
            "status": "graded" if graded else ("pending_human_review" if row["execution_mode"] == "online_model"
                                               else "not_applicable_offline_adapter"),
            "reviewer": (row.get("human_review") or {}).get("reviewer") if graded else None,
            "task_success": row.get("task_success") if graded else None,
            "appropriate_abstention": row.get("human_appropriate_abstention") if graded else None,
            "tool_failure_recovery": row.get("human_tool_failure_recovery") if graded else None,
            "semantic_correctness": "graded" if graded else None,
            "notes": (row.get("human_review") or {}).get("notes", "") if graded else "",
            "instructions": "A human reviewer must set task_success explicitly; do not infer success from metrics.",
        })
    return items


def configuration(dataset, task_validation, *, execution_mode, model=None, provider=None, provider_origin=None, params=None):
    sources = [
        "src/graph.py", "src/agents/react_agent.py", "src/agents/coordinator_agent.py", "src/agents/answer_agent.py",
        "src/retrieval.py", "src/skills/registry.py", "src/investigation.py", "src/memory/decisions.py",
        "src/llm_smoke.py", "src/investigation_context.py", "benchmark/runner.py", "benchmark/metrics.py",
        "benchmark/dataset.py", "benchmark/tasks/scoring.py", "benchmark/tasks/run_offline.py",
        "benchmark/tasks/run_online.py", "benchmark/tasks/grade.py",
    ]
    hashes = {name: hashlib.sha256((BENCHMARK_ROOT.parent / name).read_bytes()).hexdigest()
              for name in sources if (BENCHMARK_ROOT.parent / name).is_file()}
    from src.agents.react_agent import REACT_SYSTEM_PROMPT
    return {
        "snapshot_id": dataset["manifest"]["snapshot_id"],
        "task_set_version": task_validation["task_set_version"],
        "task_set_sha256": task_validation["task_set_sha256"],
        "scoring_method_version": SCORING_METHOD_VERSION,
        "evaluation_authority": {
            "task_level": TASK_LEVEL_AUTHORITY,
            "task_level_scope": "TaskFactCoverage, evidence/citation/tool metrics, manual TaskSuccessRate",
            "retrieval_gold": RETRIEVAL_GOLD_AUTHORITY,
            "retrieval_gold_scope": "frozen retrieval/gold benchmark only; expected_termination is not task-level authority",
            "comparability": "Do not compare scores across scoring_method_version or task_set_version changes without re-baselining.",
        },
        "execution_mode": execution_mode,
        "online_status": "executed" if execution_mode == "online_model" else "pending_authorization",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "provider_origin": provider_origin,
        "model": model,
        "source_sha256": hashes,
        "react_prompt_sha256": hashlib.sha256(REACT_SYSTEM_PROMPT.encode()).hexdigest(),
        "params": params or {},
        "cost_status": "unavailable_no_pricing",
        "limitations": [
            "Deterministic fact markers are not semantic correctness",
            "TaskSuccessRate is manual-only and null without explicit human grades",
            "offline_fallback is not a real model result",
        ],
    }
