"""Deterministic live evidence views, context selection and request budgeting.

Phase 3 keeps the existing contract: this module never models, never writes
Evidence and never mutates the EvidenceLedger.  It selects and compresses the
*displayed* context only.  Every retained or discarded excerpt carries a
deterministic reason, conflicts and unresolved gaps are mandatory state, and a
mandatory overflow produces a structured ``context_limit`` error instead of
silently dropping rules or scope.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from src.context_budget import (
    BUDGET_POLICY_VERSION, CONTEXT_SCHEMA_VERSION, ContextBudget,
    ContextBudgetPolicy, TokenCounter,
)
from src.evidence import Evidence

_BODY_OMITTED_REASONS = frozenset({
    "already_in_evidence_index",
    "duplicate_source_text",
    "evidence_budget",
    "request_context_byte_budget",
    "duplicate_chunk_same_evidence",
})


class Excerpt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    evidence_id: str
    content_hash: str
    project_id: str
    dataset_kind: str
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    offset_unit: str = "unicode_codepoint"

    def verify(self, parent):
        if (self.evidence_id, self.content_hash, self.project_id, self.dataset_kind) != (
                parent.evidence_id, parent.content_hash, parent.project_id, parent.dataset_kind):
            raise ValueError("Excerpt parent identity/scope mismatch")
        if not 0 <= self.char_start < self.char_end <= len(parent.content) or parent.content[self.char_start:self.char_end] != self.text:
            raise ValueError("Excerpt does not match parent content")
        if (self.line_start, self.line_end) != (parent.content.count("\n", 0, self.char_start) + 1,
                                                parent.content.count("\n", 0, self.char_end - 1) + 1):
            raise ValueError("Excerpt line locator mismatch")
        return self


def select_excerpt(parent, query, limit=700):
    """Reuse existing BM25 over overlapping character windows; parent stays intact."""
    from src.retrieval import _bm25_rank
    starts = list(range(0, max(1, len(parent.content) - limit + 1), max(1, limit // 2)))
    starts.append(max(0, len(parent.content) - limit))
    windows = [{"content": parent.content[start:start + limit], "start": start} for start in dict.fromkeys(starts)]
    chosen = (_bm25_rank(query, windows, top_k=1) or windows)[0]
    start, end = chosen["start"], chosen["start"] + len(chosen["content"])
    return Excerpt(evidence_id=parent.evidence_id, content_hash=parent.content_hash, project_id=parent.project_id,
        dataset_kind=parent.dataset_kind, text=chosen["content"], char_start=start, char_end=end,
        line_start=parent.content.count("\n", 0, start) + 1, line_end=parent.content.count("\n", 0, end - 1) + 1).verify(parent)


def source_coverage(question, ledger):
    """Only literal source mentions, not a semantic completeness certificate."""
    patterns = {"git_commit": r"\bcommits?\b|提交", "document": r"\bdocuments?\b|\bdocs?\b|文档",
                "meeting": r"\bmeetings?\b|\bminutes\b|会议|纪要",
                "issue": r"\bissues?\b|工单", "log": r"\blogs?\b|日志", "chat": r"\bchats?\b|聊天"}
    required = sorted(k for k, pattern in patterns.items() if re.search(pattern, question, re.I))
    if len(required) < 2: required = []  # avoid pretending general questions have a rigid source plan
    observed = sorted({entry.evidence.source_type for entry in ledger.entries})
    return {"method": "explicit_source_mentions", "semantic_coverage": "unverified", "required_source_types": required,
            "observed_source_types": observed, "missing_source_types": sorted(set(required) - set(observed))}


TASK_TARGETS = {
    "initial_reason": "初始原因/背景",
    "decision_formation": "决策形成",
    "review_or_objection": "评审或反对意见",
    "later_adjustment": "后续调整",
    "current_status": "当前或指定时点状态",
    "conflict_or_gap": "未解决冲突或资料缺口",
}
TARGET_ORDER = list(TASK_TARGETS)
QUESTION_TARGET_PATTERNS = {
    "initial_reason": r"为什么|为何|原因|理由|背景|动机|初衷|起因|根因|\bwhy\b|\breason\b|root cause",
    "decision_formation": r"决策|决定|方案|采用|选择|批准|结论|确定|拍板|\bdecid|\bdecision\b|\badopt|\bchos|\bapproved\b",
    "review_or_objection": r"评审|审查|\breview\b|反对|异议|意见|质疑|风险|代价|挑战|blocker|objection|concern",
    "later_adjustment": r"后来|之后|后续|变化|变更|调整|取消|替代|修订|推翻|重新评估|改成|演变|\blater\b|\bchange\b|supersed|cancel|revis|adjust",
    "current_status": r"截至|当时|当前|现在|目前|最新|仍然|仍|状态|现状|已确认|已替代|\bcurrent\b|\bnow\b|\blatest\b|\bstill\b|status|as of",
    "conflict_or_gap": r"冲突|矛盾|不一致|分歧|一致吗|是否一致|缺口|未知|不足|\bconflict\b|inconsisten|\bmissing\b|\bgap\b",
}
EVIDENCE_MARKERS = {
    "initial_reason": r"原因|理由|因为|由于|因此|所以|为了|以便|避免|防止|不足|风险|约束|代价|背景|需求|动机|根因|起因|because|due to|reason|so that|to avoid|root cause",
    "decision_formation": r"决定|决策|确定|采用|选择|批准|结论|方案|拍板|decided|decision|adopt|chosen|approved|the team will",
    "review_or_objection": r"评审|审查|review|反对|异议|质疑|意见|风险|代价|要求|建议|批准|未核对|objection|concern|blocker",
    "later_adjustment": r"后来|之后|后续|变化|变更|调整|取消|替代|修订|推翻|重新评估|改成|更新|supersed|cancel|revise|adjust|change",
    "current_status": r"当前|现在|目前|最新|仍然|仍|状态|现状|已部署|已执行|已上线|current|latest|still|status|deployed|executed",
    "conflict_or_gap": r"冲突|矛盾|不一致|分歧|未核对|缺口|未知|不足|conflict|inconsisten|missing|gap",
}
AUTHORITY_MARKERS = {
    "discussion": r"讨论|提议|草案|建议|评估|可能|尚未|未确认|proposal|draft|discussion",
    "meeting_record": r"会议结论|会议决定|会议批准|决策变更|minutes|meeting",
    "execution": r"已部署|已执行|已上线|生产已验证|deployed|executed|rolled out|in production",
}
DECISION_BINDING_STOPWORDS = {
    "采用", "方案", "决策", "决定", "讨论", "理由", "原因", "背景", "提交",
    "代码", "实现", "问题", "当前", "状态", "固定", "分区", "归属", "排序",
    "是否", "如何", "什么", "哪个", "哪些", "为什么", "怎么", "之间", "关系",
    "记录", "文档", "日志", "会议", "评审", "意见", "后续", "调整", "变更",
    "系统", "项目", "部分", "内容", "证据", "来源", "相互", "印证", "过程",
    "adopt", "adopted", "decision", "reason", "current", "status", "commit",
    "code", "implementation", "issue", "document", "history", "change",
}
CJK_SEQUENCE = re.compile(r"[㐀-鿿]+")
ASCII_TERM = re.compile(r"[a-z0-9][a-z0-9_.-]{2,}")
ID_LITERAL = re.compile(r"\b(?:dec|dv)_[0-9a-f]{32}\b", re.I)
EVIDENCE_LITERAL = re.compile(r"\bev_[0-9a-f]{16,}\b", re.I)


def _binding_terms(text, *extra_stopwords):
    """Small deterministic term set; no model, no external tokenizer."""
    lowered = str(text or "").lower()
    stop = set(DECISION_BINDING_STOPWORDS)
    stop.update(word.lower() for word in extra_stopwords if word)
    terms = set()
    for word in ASCII_TERM.findall(lowered):
        if word not in stop and not word.isdigit():
            terms.add(word)
    for sequence in CJK_SEQUENCE.findall(lowered):
        if sequence not in stop and len(sequence) >= 2:
            terms.add(sequence)
        for size in (2, 3, 4):
            for index in range(len(sequence) - size + 1):
                term = sequence[index:index + size]
                if term not in stop:
                    terms.add(term)
    return terms


def bind_decisions(question, decisions, project_id=""):
    """Bind only decisions the question explicitly or topically identifies.

    Retrieval relevance is not enough: a reviewed record for another topic must
    not mark the requested decision as observed.  An explicit decision/version
    ID is authoritative; otherwise at least two distinct non-generic topic terms
    (or one ASCII term plus another overlap) must occur in the decision text.
    """
    question_text = str(question or "")
    literals = {value.lower() for value in ID_LITERAL.findall(question_text)}
    project_terms = [part for part in re.split(r"[^A-Za-z0-9]+", project_id or "") if part]
    question_terms = _binding_terms(question_text, *project_terms)
    bound, unbound = [], []
    for decision in decisions:
        decision_id = decision.get("decision_id") or ""
        version_id = decision.get("version_id") or ""
        explicit = {decision_id.lower(), version_id.lower()} & literals
        title = decision.get("title") or ""
        statement = decision.get("statement") or ""
        decision_terms = _binding_terms(title + "\n" + statement, *project_terms)
        shared = sorted(question_terms & decision_terms)
        ascii_shared = [term for term in shared if ASCII_TERM.fullmatch(term)]
        topic_bound = len(shared) >= 2 or (len(ascii_shared) >= 1 and len(shared) >= 2)
        if explicit or topic_bound:
            bound.append({**decision, "_binding_basis": "explicit_id" if explicit else "distinctive_topic_terms",
                          "_matched_terms": sorted(set(explicit)) if explicit else shared})
        else:
            unbound.append(decision)
    return bound, unbound


def requested_task_targets(question):
    """Map the user's wording to a small target set; never a fixed full checklist."""
    targets = [target for target in TARGET_ORDER if re.search(QUESTION_TARGET_PATTERNS[target], question or "", re.I)]
    if re.search(r"完整|全过程|全流程|来龙去脉|演变|决策链|历史沿革|end-to-end", question or "", re.I):
        targets = [target for target in TARGET_ORDER if target != "conflict_or_gap"]
    return [target for target in TARGET_ORDER if target in targets]


def _matching_ids(entries, pattern, limit=5):
    return [entry.evidence.evidence_id for entry in entries
            if re.search(pattern, entry.evidence.content or "", re.I)][:limit]


def _authority_levels(entries, decisions):
    """Literal authority markers, not proof of formal confirmation or execution.

    Chat material is discussion-level even when it sounds assertive; a meeting
    record still needs meeting wording; execution status is never verified here.
    """
    levels = []
    for name, pattern in AUTHORITY_MARKERS.items():
        ids = []
        for entry in entries:
            evidence = entry.evidence
            content = evidence.content or ""
            if name == "meeting_record":
                matched = evidence.source_type == "meeting"
            elif name == "discussion":
                matched = evidence.source_type == "chat" or bool(re.search(pattern, content, re.I))
            else:
                matched = bool(re.search(pattern, content, re.I))
            if matched:
                ids.append(evidence.evidence_id)
        levels.append({"level": name, "observed": bool(ids), "evidence_ids": ids[:5],
                       "basis": "literal_source_marker" if name != "execution" else "literal_word_only_not_execution_proof"})
    levels.insert(2, {"level": "reviewed_decision", "observed": bool(decisions),
                      "version_ids": [item.get("version_id") for item in decisions][:5],
                      "basis": "topic_or_id_bound_human_reviewed_decision_version"})
    return levels


def task_coverage(question, ledger, decision_memories=(), conflicts=None, gaps=()):
    """Deterministic target/source presence checks. Not a semantic entailment judge.

    The runtime uses this to disclose missing stages and to block ``sufficient``
    when an explicitly requested target has no verifiable record. It never uses
    tool-call or evidence counts as proof of task completion.
    """
    entries = ledger.entries
    conflicts = list(ledger.conflicts if conflicts is None else conflicts)
    all_decisions = list(decision_memories)
    decisions, unbound_decisions = bind_decisions(
        question, all_decisions, getattr(ledger.scope, "project_id", ""))
    reviewed_decisions = [item for item in decisions if item.get("status") == "confirmed"]
    targets = []
    for target in requested_task_targets(question):
        ids, status, basis = [], "missing", "No deterministic marker or reviewed record was observed."
        if target == "initial_reason":
            ids = _matching_ids(entries, EVIDENCE_MARKERS[target])
            if ids:
                status, basis = "observed", "Literal causal/background marker in retrieved source; semantic validity unverified."
        elif target == "decision_formation":
            ids = _matching_ids(entries, EVIDENCE_MARKERS[target])
            if reviewed_decisions:
                ids = list(dict.fromkeys(ids + [d.get("version_id") for d in reviewed_decisions
                                                if d.get("version_id")]))[:5]
                status, basis = "observed", "Topic/ID-bound confirmed Decision Memory version present; proposal/discussion alone does not count."
            elif ids:
                status, basis = "observed_unverified", "Decision-like wording in Evidence; no bound confirmed Decision Memory version."
        elif target == "review_or_objection":
            ids = _matching_ids(entries, EVIDENCE_MARKERS[target])
            if ids:
                status, basis = "observed", "Literal review/objection marker in retrieved source; completeness unverified."
        elif target == "later_adjustment":
            ids = _matching_ids(entries, EVIDENCE_MARKERS[target])
            if ids:
                status, basis = "observed", "Literal later-change marker in retrieved source; chronology not semantically verified."
        elif target == "current_status":
            current = [d for d in decisions if d.get("system_current")]
            ids = _matching_ids(entries, EVIDENCE_MARKERS[target])
            if current:
                ids = list(dict.fromkeys([d.get("version_id") for d in current] + ids))[:5]
                status, basis = "observed_system_record", "Reviewed Decision Memory current pointer; external world state remains unknown."
            elif ids:
                status, basis = "observed_unverified", "Literal status wording in Evidence; external/current execution remains unverified."
        else:
            decision_conflicts = [c for d in decisions for c in d.get("decision_conflicts", [])
                                  if c.get("resolution_status") == "open"]
            if conflicts or decision_conflicts:
                status, basis = "observed", "Conflict records retained; no side was chosen automatically."
                ids = [entry.evidence.evidence_id for entry in entries][:5]
            elif any(re.search("缺口|未知|不足|missing|gap", gap, re.I) for gap in gaps):
                status, basis = "observed", "An explicit unresolved gap is present."
            else:
                basis = "No conflict or explicit gap was recorded by the retrieval run; this is not proof that none exists."
        targets.append({"target": target, "label": TASK_TARGETS[target], "status": status,
                        "basis": basis, "supporting_evidence_ids": ids})
    missing = [item["target"] for item in targets if item["status"] == "missing"]
    source = source_coverage(question, ledger)
    identities = {(entry.evidence.evidence_id, entry.evidence.content_hash) for entry in entries}
    records = {(entry.evidence.source_uri, entry.evidence.original_record_id) for entry in entries}
    return {
        **source,
        "method": "deterministic_goal_and_literal_marker_check",
        "semantic_completeness": "unverified",
        "targets": targets,
        "missing_targets": missing,
        "unverified_targets": [item["target"] for item in targets if item["status"] == "observed_unverified"],
        "authority_levels": _authority_levels(entries, reviewed_decisions),
        "decision_binding": {
            "method": "explicit_decision_or_version_id_or_distinctive_topic_terms",
            "semantic_match": "unverified",
            "bound": [{"decision_id": item.get("decision_id"),
                       "version_ids": [item.get("version_id")],
                       "basis": item.get("_binding_basis"),
                       "matched_terms": item.get("_matched_terms", [])[:10]}
                      for item in decisions],
            "unbound_version_ids": [item.get("version_id") for item in unbound_decisions][:20],
        },
        "distinct_evidence_count": len(identities),
        "independent_record_count": len(records),
        "duplicate_evidence_observations": getattr(ledger, "duplicate_observations", 0),
        "completion_basis": "record_presence_only_not_tool_or_evidence_count",
    }


class ContextLimitError(ValueError):
    """Backward-compatible base for byte and token mandatory overflows."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = dict(detail or {})
        self.detail.setdefault("context_limit", {
            "code": "context_limit", "reason": "mandatory_state_exceeds_budget"})
        self.detail.setdefault("code", "context_limit")


class ContextOverflowError(ContextLimitError):
    """Token/byte mandatory overflow with structured ``context_limit`` detail."""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _identity(view) -> tuple:
    return (view.get("evidence_id"), view.get("content_hash"))


def _view_basis(view) -> str:
    return str(view.get("excerpt_basis") or "verified_parent_excerpt")


def _view_content(view) -> str:
    return str(view.get("content") or "")


def _short(value, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _public_view(view: dict) -> dict:
    """Strip selection-only bookkeeping before a view enters the JSON request."""
    allowed = ("evidence_id", "content_hash", "project_id", "dataset_kind",
               "source_type", "source_uri", "event_time", "locator", "time_status",
               "legacy_unverified", "retrieval_score", "retrieval_score_kind",
               "retrieval_relevance", "excerpt_basis", "source_kind", "excerpt",
               "content", "body_omitted")
    return {key: copy.deepcopy(view[key]) for key in allowed if key in view}


class InvestigationContext:
    """Retain identities/locators/tool state; at most one copy of each excerpt per request.

    ``observe`` stores only verified model views.  ``request`` builds a
    versioned, priority-pruned request and returns statistics; raw Evidence and
    the ledger are never modified.
    """

    def __init__(self):
        self.views = {}

    @staticmethod
    def key(view):
        excerpt = view.get("excerpt", {})
        return (view["evidence_id"], view["content_hash"],
                excerpt.get("char_start"), excerpt.get("char_end"))

    def observe(self, items):
        result = []
        for item in items:
            parent = Evidence.model_validate({k: item[k] for k in Evidence.model_fields if k in item})
            snippet = Excerpt.model_validate(item["excerpt"]).verify(parent)
            view = {"evidence_id": parent.evidence_id, "content_hash": parent.content_hash,
                "project_id": parent.project_id, "dataset_kind": parent.dataset_kind,
                "source_type": parent.source_type,
                "source_uri": parent.source_uri, "event_time": item.get("event_time"),
                "locator": parent.locator,
                "time_status": "unknown" if parent.event_time is None else "recorded",
                "legacy_unverified": bool(parent.metadata.get("legacy")),
                "retrieval_score": float(item.get("score") or 0.0),
                "retrieval_score_kind": item.get("score_kind") or "retrieval_relevance",
                "retrieval_relevance": item.get("relevance"),
                "excerpt_basis": item.get("excerpt_basis") or "verified_excerpt",
                "source_kind": "verified_evidence_excerpt",
                "excerpt": snippet.model_dump(exclude={"text"}), "content": snippet.text}
            key = self.key(view)
            if key in self.views:
                result.append({**view, "content": "", "body_omitted": "already_in_evidence_index"})
            else:
                self.views[key] = view
                result.append(view)
        return result

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    @staticmethod
    def _conflict_keys(ledger) -> set[tuple]:
        keys = set()
        for conflict in getattr(ledger, "conflicts", []) or []:
            for ref in getattr(conflict, "refs", []) or []:
                keys.add((ref.evidence_id, ref.content_hash))
        return keys

    @staticmethod
    def _claim_keys(claims) -> set[tuple]:
        keys = set()
        for claim in claims or []:
            refs = getattr(claim, "refs", None)
            if refs is None and isinstance(claim, dict):
                refs = claim.get("refs") or []
            for ref in refs or []:
                evidence_id = getattr(ref, "evidence_id", None)
                content_hash = getattr(ref, "content_hash", None)
                if isinstance(ref, dict):
                    evidence_id = ref.get("evidence_id")
                    content_hash = ref.get("content_hash")
                if evidence_id and content_hash:
                    keys.add((evidence_id, content_hash))
        return keys

    @staticmethod
    def _coverage_keys(coverage) -> set[tuple]:
        keys = set()
        for target in (coverage or {}).get("targets", []) or []:
            if target.get("status") in {"missing", "observed_unverified"}:
                continue
            for evidence_id in target.get("supporting_evidence_ids", []) or []:
                keys.add((evidence_id, None))
        return keys

    @staticmethod
    def _explicit_keys(question, views) -> set[tuple]:
        text = str(question or "")
        identifiers = {value.lower() for value in EVIDENCE_LITERAL.findall(text)}
        keys = set()
        if identifiers:
            for view in views:
                if str(view.get("evidence_id") or "").lower() in identifiers:
                    keys.add(_identity(view))
        # A precise source/record locator in the user question is also explicit.
        for view in views:
            for field in ("source_uri", "original_record_id"):
                value = str(view.get(field) or "")
                if len(value) >= 4 and value in text:
                    keys.add(_identity(view))
        return keys

    @staticmethod
    def _relevance(view, question) -> float:
        score = view.get("retrieval_score")
        try:
            return float(score or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _candidates(self, latest_views):
        """Unique verified excerpts, one candidate per chunk; duplicates marked."""
        candidates = []
        by_key = {}
        for view in list(self.views.values()) + list(latest_views or []):
            copied = copy.deepcopy(view)
            key = self.key(copied)
            if key in by_key:
                existing = by_key[key]
                if not _view_content(existing) and _view_content(copied):
                    by_key[key] = existing = copied
                existing.setdefault("_duplicate_keys", set()).add(key)
                continue
            copied["_key"] = key
            by_key[key] = copied
            candidates.append(copied)
        return candidates

    def _priority(self, candidate, *, conflict_keys, claim_keys, explicit_keys,
                  coverage_keys, question) -> tuple[int, str]:
        identity = _identity(candidate)
        coverage_match = any(identity[0] == value[0] for value in coverage_keys)
        if identity in conflict_keys:
            return 0, "critical_conflict_evidence"
        if identity in claim_keys:
            return 0, "verified_claim_support"
        if identity in explicit_keys:
            return 0, "explicit_question_reference"
        if coverage_match:
            return 1, "requested_coverage_support"
        if _view_content(candidate) and self._relevance(candidate, question) > 0:
            return 2, "question_relevance"
        return 3, "retained_evidence_index"

    def select_evidence(self, *, views, ledger, question, claims=(), coverage=None,
                        counter, token_cap: int, max_excerpts: int,
                        max_per_evidence: int) -> dict:
        """Deterministic priority selection; similarity is ordering, not credibility."""
        conflict_keys = self._conflict_keys(ledger)
        claim_keys = self._claim_keys(claims)
        explicit_keys = self._explicit_keys(question, views)
        coverage_keys = self._coverage_keys(coverage)
        decisions = []
        grouped = {}
        for candidate in views:
            identity = _identity(candidate)
            grouped.setdefault(identity, []).append(candidate)
        normalized = []
        deduped = 0
        for identity, group in grouped.items():
            ranked = sorted(group, key=lambda item: (
                self._priority(item, conflict_keys=conflict_keys, claim_keys=claim_keys,
                               explicit_keys=explicit_keys, coverage_keys=coverage_keys,
                               question=question),
                -self._relevance(item, question),
                counter.count_text(_view_content(item)),
                self.key(item)))
            best = ranked[0]
            normalized.append(best)
            for item in ranked[1:]:
                deduped += 1
                decisions.append({"evidence_id": identity[0], "content_hash": identity[1],
                                  "decision": "dropped", "reason": "duplicate_chunk_same_evidence",
                                  "char_start": item.get("excerpt", {}).get("char_start"),
                                  "char_end": item.get("excerpt", {}).get("char_end")})
                best.setdefault("_duplicate_keys", set()).add(self.key(item))

        for candidate in normalized:
            priority, reason = self._priority(
                candidate, conflict_keys=conflict_keys, claim_keys=claim_keys,
                explicit_keys=explicit_keys, coverage_keys=coverage_keys, question=question)
            candidate["_priority"] = priority
            candidate["_reason"] = reason
        normalized.sort(key=lambda item: (
            item["_priority"], -self._relevance(item, question),
            counter.count_text(_view_content(item)), self.key(item)))

        selected, index_only = [], []
        bodies = 0
        per_evidence = {}
        seen_text = {}
        used_tokens = 0
        for candidate in normalized:
            identity = _identity(candidate)
            excerpt_key = self.key(candidate)
            estimate = counter.count_text(_view_content(candidate))
            chosen = candidate
            chosen_reason = candidate["_reason"]
            if identity in conflict_keys or identity in claim_keys:
                # Critical constraints always keep their identity; bodies are
                # best-effort and never crowd out a lower-priority conflict.
                pass
            if max_per_evidence and per_evidence.get(identity, 0) >= max_per_evidence:
                index_only.append((chosen, "duplicate_chunk_same_evidence"))
                continue
            if estimate <= 0:
                index_only.append((chosen, "empty_excerpt_body"))
                continue
            text_digest = _sha256_text(_view_content(candidate))
            if text_digest in seen_text and identity not in conflict_keys and identity not in claim_keys:
                chosen["body_omitted"] = "duplicate_source_text"
                index_only.append((chosen, "duplicate_source_text"))
                continue
            if len(selected) >= max_excerpts or (used_tokens + estimate > token_cap and selected):
                index_only.append((chosen, "evidence_budget"))
                continue
            if used_tokens + estimate > token_cap and not selected:
                index_only.append((chosen, "evidence_budget"))
                continue
            selected.append(chosen)
            per_evidence[identity] = per_evidence.get(identity, 0) + 1
            seen_text[text_digest] = excerpt_key
            used_tokens += estimate
            decisions.append({"evidence_id": identity[0], "content_hash": identity[1],
                              "decision": "retained", "reason": chosen_reason,
                              "priority": candidate["_priority"],
                              "estimated_excerpt_tokens": estimate,
                              "excerpt_basis": _view_basis(candidate),
                              "char_start": candidate.get("excerpt", {}).get("char_start"),
                              "char_end": candidate.get("excerpt", {}).get("char_end")})
        for chosen, reason in index_only:
            if not _view_content(chosen):
                continue
            chosen["content"] = ""
            chosen.setdefault("body_omitted", reason)
            decisions.append({"evidence_id": _identity(chosen)[0],
                              "content_hash": _identity(chosen)[1],
                              "decision": "index_only", "reason": reason,
                              "excerpt_basis": _view_basis(chosen),
                              "char_start": chosen.get("excerpt", {}).get("char_start"),
                              "char_end": chosen.get("excerpt", {}).get("char_end")})
        reasons = {}
        for item in decisions:
            if item["decision"] != "retained":
                reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        return {"selected": selected, "index_only": index_only,
                "decisions": decisions, "deduped_excerpts": deduped,
                "selected_tokens": used_tokens, "pruning_reasons": reasons,
                "candidate_count": len(views),
                "unique_evidence_count": len(grouped)}

    # ------------------------------------------------------------------
    # Request construction
    # ------------------------------------------------------------------

    @staticmethod
    def _split_user_content(content: str):
        text = str(content or "")
        marker = "\n\n[会话上下文"
        index = text.find(marker)
        if index < 0:
            return text, None
        end = text.rfind("[会话上下文结束]")
        return text[:index], text[index + 2:end + len("[会话上下文结束]")]

    @staticmethod
    def _session_stats(session_text):
        if not session_text:
            return {"present": False, "previous_investigations": 0, "verified_evidence_refs": 0}
        try:
            payload = json.loads(session_text)
        except (ValueError, TypeError):
            return {"present": True, "previous_investigations": 0, "verified_evidence_refs": 0}
        investigations = payload.get("previous_investigations") or []
        return {"present": True, "previous_investigations": len(investigations),
                "verified_evidence_refs": sum(len(item.get("verified_evidence_refs") or [])
                                              for item in investigations)}

    @staticmethod
    def _trim_session(session_text, counter, cap_tokens):
        """Bound follow-up history without changing its non-factual status."""
        if not session_text:
            return None, {"kept": 0, "summarized": 0, "dropped": 0}
        try:
            payload = json.loads(session_text)
        except (ValueError, TypeError):
            bounded = str(session_text)
            while counter.count_text(bounded) > cap_tokens and len(bounded) > 200:
                bounded = bounded[:-200]
            return bounded, {"present": True, "kept": 0, "summarized": 0,
                             "dropped": 0,
                             "reason": "unparseable_session_context_truncated"}
        investigations = list(payload.get("previous_investigations") or [])
        summarized = dropped = 0
        while investigations and counter.count_text(
                json.dumps({**payload, "previous_investigations": investigations},
                           ensure_ascii=False)) > cap_tokens:
            oldest = investigations[0]
            compact = {"run_id": oldest.get("run_id"),
                       "question": _short(oldest.get("question"), 160),
                       "status": oldest.get("status"),
                       "summary_kind": "compressed_session_history",
                       "prior_answer_hint_omitted": True,
                       "unresolved_gap_count": len(oldest.get("unresolved_gaps") or []),
                       "conflict_count": len(oldest.get("conflicts") or []),
                       "verified_evidence_refs": (oldest.get("verified_evidence_refs")
                                                  or [])[:5],
                       "verified_ref_count": len(oldest.get("verified_evidence_refs") or [])}
            if oldest.get("summary_kind") == "compressed_session_history":
                investigations.pop(0)
                dropped += 1
            else:
                investigations[0] = compact
                summarized += 1
        if not investigations:
            return None, {"present": True, "kept": 0,
                          "summarized": summarized, "dropped": dropped,
                          "reason": "session_budget"}
        rendered = json.dumps({**payload, "previous_investigations": investigations},
                              ensure_ascii=False)
        return rendered, {"present": True, "kept": len(investigations),
                          "summarized": summarized, "dropped": dropped,
                          "reason": "session_budget" if summarized or dropped else None}

    @staticmethod
    def _tool_history(trace, detailed=True):
        """Group attempts and keep failures/empty searches visible after compression."""
        groups = {}
        for step in trace or []:
            if not (getattr(step, "tool_name", None) or getattr(step, "errors", None)):
                continue
            if getattr(step, "action", "") != "tool_attempt":
                continue
            filters = dict(getattr(step, "filters", {}) or {})
            signature = (getattr(step, "tool_name", None), getattr(step, "query", "") or "",
                         json.dumps(filters, sort_keys=True, ensure_ascii=False))
            group = groups.setdefault(signature, {
                "tool": getattr(step, "tool_name", None),
                "query": _short(getattr(step, "query", ""), 240 if detailed else 120),
                "filters": filters if detailed else {
                    key: value for key, value in filters.items()
                    if key in {"after", "before", "evidence_ids", "undated_only",
                               "source_types", "known_at", "memory_view", "decision_id"}},
                "attempts": 0, "evidence_count": 0, "new_evidence": False,
                "failures": [], "empty": False, "recoveries": [],
                "last_status": "ok", "evidence_ids": []})
            group["attempts"] += 1
            errors = list(getattr(step, "errors", []) or [])
            if errors:
                group["last_status"] = "failed"
                for error in errors:
                    group["failures"].append({
                        "error_code": error.error_code,
                        "retryable": error.retryable,
                        "message": _short(error.message, 160),
                    })
            ids = list(getattr(step, "evidence_ids", []) or [])
            group["evidence_ids"] = list(dict.fromkeys(group["evidence_ids"] + ids))[:8]
            group["evidence_count"] += len(ids)
            group["empty"] = group["evidence_count"] == 0 and not errors
            if getattr(step, "recovery_kind", None):
                group["recoveries"].append(step.recovery_kind)
            if getattr(step, "new_evidence", False):
                group["new_evidence"] = True
        history = []
        for group in groups.values():
            group["evidence_ids"] = group["evidence_ids"][:8]
            history.append(group)
        history.sort(key=lambda item: (item.get("tool") or "", item.get("query") or "",
                                       json.dumps(item.get("filters") or {}, sort_keys=True)))
        return history

    @staticmethod
    def _empty_searches(tool_history):
        return [{"tool": item["tool"], "query": item["query"],
                 "filters": item["filters"],
                 "reason": "no_evidence_after_retrieval_and_filters",
                 "recovery": item["recoveries"]}
                for item in tool_history if item["empty"]][:12]

    @staticmethod
    def _trim_coverage(coverage, detailed=True):
        if not coverage:
            return {}
        core = {}
        for key in ("method", "semantic_completeness", "completion_basis",
                    "required_source_types", "observed_source_types",
                    "missing_source_types", "missing_targets", "unverified_targets"):
            value = coverage.get(key)
            if isinstance(value, list):
                if value:
                    core[key] = value[:12]
            elif value:
                core[key] = value
        targets = [{"target": item.get("target"), "status": item.get("status"),
                    "supporting_evidence_ids": item.get("supporting_evidence_ids", [])[:5]}
                   for item in coverage.get("targets", []) or []]
        if targets:
            core["targets"] = targets
        for key in ("distinct_evidence_count", "independent_record_count"):
            if coverage.get(key):
                core[key] = coverage[key]
        if detailed:
            observed = [level for level in coverage.get("authority_levels", []) or []
                        if level.get("observed")]
            if observed:
                core["authority_levels"] = observed
            binding = coverage.get("decision_binding") or {}
            if binding.get("bound"):
                core["decision_binding"] = {
                    "semantic_match": binding.get("semantic_match"),
                    "bound": binding.get("bound", [])[:10],
                }
        return core

    @staticmethod
    def _trim_memories(memories, detailed=True):
        items = []
        for memory in (memories.values() if isinstance(memories, dict) else memories or []):
            if not isinstance(memory, dict):
                continue
            item = {"version_id": memory.get("version_id"),
                    "decision_id": memory.get("decision_id"),
                    "title": _short(memory.get("title"), 160),
                    "status": memory.get("status"),
                    "system_current": bool(memory.get("system_current")),
                    "source_kind": "reviewed_decision_memory_not_raw_evidence",
                    "evidence_refs": [{"evidence_id": ref.get("evidence_id"),
                                       "content_hash": ref.get("content_hash")}
                                      for ref in (memory.get("evidence_refs") or [])[:5]]}
            if detailed:
                item["statement"] = _short(memory.get("statement"), 300)
            items.append(item)
        items.sort(key=lambda item: str(item.get("version_id") or ""))
        return items[:8]

    @staticmethod
    def _trim_claims(claims):
        items = []
        for claim in claims or []:
            text = getattr(claim, "text", None)
            refs = getattr(claim, "refs", None)
            if text is None and isinstance(claim, dict):
                text, refs = claim.get("text"), claim.get("refs") or []
            items.append({"text": _short(text, 400),
                          "source_kind": "validated_claim_from_this_run",
                          "refs": [{"evidence_id": getattr(ref, "evidence_id", None)
                                    if not isinstance(ref, dict) else ref.get("evidence_id"),
                                    "content_hash": getattr(ref, "content_hash", None)
                                    if not isinstance(ref, dict) else ref.get("content_hash")}
                                   for ref in (refs or [])]})
        return items[:20]

    @staticmethod
    def _trim_conflicts(ledger):
        items = []
        for conflict in getattr(ledger, "conflicts", []) or []:
            items.append({
                "description": _short(conflict.description, 500),
                "relation": getattr(conflict, "relation", None),
                "origin": getattr(conflict, "origin", None),
                "fields": list(getattr(conflict, "fields", []) or [])[:20],
                "refs": [{"evidence_id": ref.evidence_id,
                          "content_hash": ref.content_hash}
                         for ref in conflict.refs],
                "retained": True,
                "adjudication": "not_performed",
            })
        return items

    @classmethod
    def _recent_messages(cls, messages, state_keys=(),
                         state_texts=(), *, selected_keys=None,
                         selected_texts=()):
        """Latest action/observation, deduplicated against the evidence index.

        With ``selected_keys`` the latest observation is selection-aware: a
        selected chunk keeps its body in the observation (unless the same body
        is already in structured state), while unselected chunks become
        index-only references with an explicit reason.  Without selection the
        older floor behaviour is preserved.
        """
        recent = copy.deepcopy(
            messages[-2:] if len(messages) > 3 and messages[-2].get("role") == "assistant"
            else messages[2:][-1:])
        state_keys = set(state_keys or ())
        state_texts = set(state_texts or ())
        selected_keys = None if selected_keys is None else set(selected_keys)
        selected_texts = set(selected_texts or ())
        for message in recent:
            if message.get("role") != "user":
                continue
            try:
                payload = json.loads(message.get("content") or "")
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            views = payload.get("evidence")
            if isinstance(views, list):
                for view in views:
                    if not isinstance(view, dict):
                        continue
                    body = _view_content(view)
                    if not body:
                        continue
                    key = cls.key(view)
                    digest = _sha256_text(body)
                    if selected_keys is not None and key not in selected_keys:
                        # Same text under a different chunk/evidence is still a
                        # duplicate display; the selected body is the one copy.
                        view["content"] = ""
                        view["body_omitted"] = ("duplicate_source_text"
                                                if digest in selected_texts
                                                else "evidence_budget")
                    elif key in state_keys or digest in state_texts:
                        view["content"] = ""
                        view["body_omitted"] = "already_in_evidence_index"
            message["content"] = json.dumps(payload, ensure_ascii=False)
        return recent

    def request(self, messages, ledger, trace, memories, coverage, max_bytes,
                gaps=(), *, claims=(), question=None, session_context=None,
                context_budget: ContextBudget | None = None,
                model: str = "") -> tuple[list[dict], dict]:
        """Build a deterministic request and its statistics.

        ``max_bytes`` is the auxiliary Phase 2 ceiling.  When ``context_budget``
        is absent, a model-agnostic token policy is derived from that ceiling so
        direct callers keep a safe token-aware plan too.
        """
        if context_budget is None:
            policy = ContextBudgetPolicy(
                model=model, model_context_window=64000, reserved_output_tokens=0,
                safety_margin_tokens=0, max_context_bytes=max(1, int(max_bytes)))
            counter = TokenCounter(model, "estimate")
            context_budget = ContextBudget(policy, counter, remaining_total_tokens=10**9)
        else:
            counter = context_budget.counter
        ceiling = context_budget.input_ceiling_tokens
        policy = context_budget.policy

        original_user = str(messages[1].get("content") or "")
        parsed_question, embedded_session = self._split_user_content(original_user)
        if question is None:
            question = parsed_question
        if session_context is None:
            session_context = embedded_session

        # Session context shares the same ceiling; it never displaces mandatory
        # question/investigation state or the evidence index.
        session_cap = max(120, int(ceiling * policy.session_share))
        trimmed_session, session_stats = self._trim_session(
            session_context, counter, session_cap)
        user_question = parsed_question if question is None else str(question)
        if trimmed_session:
            user_content = (f"{user_question}\n\n"
                            "[会话上下文：仅用于理解追问，不是事实来源；任何结论必须由本次运行重新取得并校验Evidence]\n"
                            f"{trimmed_session}\n[会话上下文结束]")
        else:
            user_content = user_question
        system_content = str(messages[0].get("content") or "")

        latest_views = []
        raw_recent = self._recent_source(messages)
        if raw_recent:
            try:
                payload = json.loads(raw_recent.get("content") or "")
                latest_views = payload.get("evidence", []) if isinstance(payload, dict) else []
            except (ValueError, TypeError, AttributeError):
                latest_views = []

        views = self._candidates(latest_views)
        tool_history = self._tool_history(trace)
        empty_searches = self._empty_searches(tool_history)
        conflicts = self._trim_conflicts(ledger)
        gap_list = list(dict.fromkeys(list(getattr(ledger, "gaps", []) or []) + list(gaps or [])))
        gap_list = [_short(gap, 500) for gap in gap_list]

        index = []
        for entry in ledger.entries:
            parent = entry.evidence
            index.append({**parent.model_dump(mode="json", exclude={"content", "metadata", "ingested_at"}),
                          "time_status": "unknown" if parent.event_time is None else "recorded",
                          "source_kind": "verified_evidence_identity",
                          "excerpts": [copy.deepcopy(v["excerpt"]) for k, v in self.views.items()
                                       if k[:2] == (parent.evidence_id, parent.content_hash)]})
        index.sort(key=lambda item: (str(item.get("evidence_id")), str(item.get("content_hash"))))

        base_state = {
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "budget_policy_version": BUDGET_POLICY_VERSION,
            "task": {
                "goal": user_question,
                "scope": {"project_id": ledger.scope.project_id,
                          "dataset_kind": ledger.scope.dataset_kind},
                "requested_targets": requested_task_targets(user_question),
                "required_source_types": (coverage or {}).get("required_source_types", []),
                "instruction": "只使用本次运行校验过的证据；历史回答与检索相似度都不是事实证明。",
            },
            "evidence_index": index,
            "verified_claims": self._trim_claims(claims),
            "gaps": gap_list,
            "gap_status": "unresolved",
            "conflicts": conflicts,
            "tool_failures": [{"tool": item["tool"], "query": item["query"],
                               "failures": item["failures"]}
                              for item in tool_history if item["failures"]][:12],
            "attempted_empty_searches": empty_searches,
        }
        if not base_state["verified_claims"]:
            base_state.pop("verified_claims")
        if not base_state["conflicts"]:
            base_state.pop("conflicts")
        if not base_state["tool_failures"]:
            base_state.pop("tool_failures")
        if not base_state["attempted_empty_searches"]:
            base_state.pop("attempted_empty_searches")
        if not base_state["task"]["requested_targets"]:
            base_state["task"].pop("requested_targets")
        if not base_state["task"]["required_source_types"]:
            base_state["task"].pop("required_source_types")
        if session_stats.get("present"):
            base_state["session_context_meta"] = {
                **self._session_stats(trimmed_session),
                **{key: value for key, value in session_stats.items()
                   if key != "present"},
                "fact_source": False,
                "note": "历史回答不是事实来源。"}
        state = copy.deepcopy(base_state)
        completed_steps = [
            {"step": int(getattr(step, "step_index", 0)),
             "tool": getattr(step, "tool_name", None),
             "evidence_count": len(getattr(step, "evidence_ids", []) or []),
             "new_evidence": bool(getattr(step, "new_evidence", False)),
             "recovery": getattr(step, "recovery_kind", None)}
            for step in trace or []
            if getattr(step, "action", "") == "tool_attempt"][-12:]
        if completed_steps:
            state["completed_steps"] = completed_steps
        if tool_history:
            state["tool_history"] = tool_history
        memories_state = self._trim_memories(memories)
        if memories_state:
            state["decision_memories"] = memories_state
        coverage_state = self._trim_coverage(coverage)
        if coverage_state:
            state["task_coverage"] = coverage_state

        def render(state_value, recent_value, user_value):
            return ([{"role": "system", "content": system_content},
                     {"role": "user", "content": user_value},
                     {"role": "user", "content": json.dumps(state_value, ensure_ascii=False)}]
                    + list(recent_value or []))

        def size(value):
            return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

        # Mandatory floor: no excerpt bodies, no optional history detail.
        floor_state = copy.deepcopy(base_state)
        if tool_history:
            floor_state["tool_history"] = [
                {"tool": item["tool"], "query": item["query"], "attempts": item["attempts"],
                 "evidence_count": item["evidence_count"], "failures": item["failures"],
                 "empty": item["empty"]}
                for item in tool_history]
        floor_state.pop("decision_memories", None)
        coverage_floor = self._trim_coverage(coverage, detailed=False)
        if coverage_floor:
            floor_state["task_coverage"] = coverage_floor
        else:
            floor_state.pop("task_coverage", None)
        # The mandatory floor never counts optional excerpt bodies: evidence is
        # selected afterwards against the remaining ceiling.  Locators, error
        # state, gaps and scope remain in the floor request.
        floor_recent = self._recent_messages(
            messages, set(), set(), selected_keys=set(), selected_texts=set())
        floor_request = render(floor_state, floor_recent, user_content)
        floor_tokens = counter.count_messages(floor_request)
        floor_bytes = size(floor_request)
        if floor_tokens > ceiling:
            raise ContextOverflowError(
                "Mandatory system/question/index/tool-failure state exceeds the token budget",
                {**policy.trace(), **counter.trace(),
                 "context_schema_version": CONTEXT_SCHEMA_VERSION,
                 "budget_policy_version": BUDGET_POLICY_VERSION,
                 "estimated_input_tokens": floor_tokens,
                 "input_token_ceiling": ceiling,
                 "input_bytes": floor_bytes,
                 "limit_bytes": max_bytes,
                 "context_limit": {"code": "context_limit",
                                   "reason": "mandatory_state_exceeds_budget",
                                   "category": "mandatory_state",
                                   "required_input_tokens": floor_tokens,
                                   "available_input_tokens": ceiling}})
        remaining = max(0, ceiling - floor_tokens)
        evidence_cap = max(0, int(remaining * policy.evidence_share))
        selection = self.select_evidence(
            views=views, ledger=ledger, question=user_question, claims=claims,
            coverage=coverage, counter=counter, token_cap=evidence_cap,
            max_excerpts=policy.max_excerpts,
            max_per_evidence=policy.max_excerpts_per_evidence)
        index_by_identity = {(item["evidence_id"], item["content_hash"]): item
                             for item in state["evidence_index"]}
        decisions_by_identity = {}
        for decision in selection["decisions"]:
            decisions_by_identity.setdefault(
                (decision["evidence_id"], decision["content_hash"]), []).append({
                    "decision": decision["decision"], "reason": decision["reason"]})
        for view in selection["selected"]:
            item = index_by_identity.get(_identity(view))
            if item is not None:
                item["selection"] = "retained"
                item["selection_reason"] = view.get("_reason")
                item["excerpt_basis"] = _view_basis(view)
                item["retrieval_score_kind"] = view.get("retrieval_score_kind")
                item["retained_excerpt"] = view.get("excerpt")
        for identity, decisions in decisions_by_identity.items():
            item = index_by_identity.get(identity)
            if item is not None and item.get("selection") != "retained":
                item["selection"] = decisions[0]["decision"]
                item["selection_reason"] = decisions[0]["reason"]
        state["retained_excerpts"] = [
            {**_public_view(view), "content_kind": "verified_excerpt",
             "selection_reason": view.get("_reason")}
            for view in selection["selected"]]
        # The observation that introduced a verified chunk is itself part of the
        # request.  Keep its body there and show the index entry instead, so the
        # same body never appears twice.
        latest_body_keys = {self.key(view) for view in latest_views
                            if _view_content(view)}
        for view in state["retained_excerpts"]:
            if self.key(view) in latest_body_keys and _view_content(view):
                view["content"] = ""
                view["body_omitted"] = "present_in_latest_observation"
                for item in index_by_identity.values():
                    if (item.get("evidence_id"), item.get("content_hash")) == _identity(view):
                        item["selection"] = "retained_in_observation"
                        item["selection_reason"] = view.get("selection_reason")
        selected_bodies = {self.key(view) for view in state["retained_excerpts"]
                           if _view_content(view)}
        selected_texts = {_sha256_text(_view_content(view))
                          for view in state["retained_excerpts"] if _view_content(view)}
        selected_keys_all = {self.key(view) for view in selection["selected"]}
        selected_texts_all = {_sha256_text(_view_content(view))
                              for view in selection["selected"]
                              if _view_content(view)}
        # Keep the evidence index first, then the retained excerpt bodies.
        # Chunks that lost selection keep only their locator and a reason.
        recent = self._recent_messages(
            messages, selected_bodies, selected_texts,
            selected_keys=selected_keys_all,
            selected_texts=selected_texts_all)
        request = render(state, recent, user_content)

        pruning_reasons = dict(selection["pruning_reasons"])
        pruned_count = sum(pruning_reasons.values())
        omitted_bodies = 0
        for view in state["retained_excerpts"]:
            if not _view_content(view):
                omitted_bodies += 1

        def over_tokens():
            return counter.count_messages(request) > ceiling

        def over_bytes():
            return size(request) > max_bytes

        def _recent_body_candidates(reason_by_key):
            candidates = []
            for message in recent:
                if message.get("role") != "user":
                    continue
                try:
                    payload = json.loads(message.get("content") or "")
                except (ValueError, TypeError):
                    continue
                for view in (payload.get("evidence")
                             if isinstance(payload, dict) else None) or []:
                    if not isinstance(view, dict) or not _view_content(view):
                        continue
                    key = self.key(view)
                    candidates.append({
                        "kind": "recent", "view": view, "key": key,
                        "message": message, "payload": payload,
                        "reason": reason_by_key.get(key, "question_relevance")})
            return candidates

        def drop_one_excerpt(reason):
            nonlocal omitted_bodies, pruned_count
            # Lowest priority first; conflict/claim evidence is last.
            reason_by_key = {self.key(view): view.get("selection_reason")
                             for view in state["retained_excerpts"]}
            candidates = [
                {"kind": "state", "view": view, "key": self.key(view),
                 "reason": view.get("selection_reason")}
                for view in state["retained_excerpts"] if _view_content(view)]
            candidates += _recent_body_candidates(reason_by_key)
            if not candidates:
                return False
            rank = {"critical_conflict_evidence": 3,
                    "verified_claim_support": 3}
            candidates.sort(key=lambda item: (
                rank.get(item["reason"], 0), item["kind"] != "state"))
            victim = candidates[0]
            view = victim["view"]
            view["content"] = ""
            view["body_omitted"] = reason
            if victim["kind"] == "recent":
                victim["message"]["content"] = json.dumps(
                    victim["payload"], ensure_ascii=False)
            omitted_bodies += 1
            pruned_count += 1
            pruning_reasons[reason] = pruning_reasons.get(reason, 0) + 1
            for item in state["evidence_index"]:
                if (item.get("evidence_id"), item.get("content_hash")) == _identity(view):
                    item["selection"] = "index_only"
                    item["selection_reason"] = reason
            update_recent_bodies()
            return True

        def update_recent_bodies():
            # A retained body must never appear twice in one request.
            bodies = {self.key(view): _sha256_text(_view_content(view))
                      for view in state["retained_excerpts"] if _view_content(view)}
            for message in recent:
                if message.get("role") != "user":
                    continue
                try:
                    payload = json.loads(message.get("content") or "")
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                for view in payload.get("evidence") or []:
                    if not isinstance(view, dict) or not _view_content(view):
                        continue
                    key = self.key(view)
                    if key in bodies or _sha256_text(_view_content(view)) in bodies.values():
                        view["content"] = ""
                        view.setdefault("body_omitted", "already_in_evidence_index")
                message["content"] = json.dumps(payload, ensure_ascii=False)

        def prune_tool_history():
            nonlocal pruned_count
            compact = self._tool_history(trace, detailed=False)
            if state.get("tool_history") and state["tool_history"] != compact:
                state["tool_history"] = compact
                pruned_count += 1
                pruning_reasons["tool_history_compressed"] = \
                    pruning_reasons.get("tool_history_compressed", 0) + 1
                return True
            if state.get("completed_steps"):
                steps = state["completed_steps"]
                if len(steps) > 4:
                    state["completed_steps"] = steps[-4:]
                else:
                    state.pop("completed_steps")
                pruned_count += 1
                pruning_reasons["completed_steps_bounded"] = \
                    pruning_reasons.get("completed_steps_bounded", 0) + 1
                return True
            return False

        def prune_recent():
            nonlocal pruned_count
            changed = False
            for message in recent:
                if message.get("role") != "user":
                    continue
                try:
                    payload = json.loads(message.get("content") or "")
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                for key in ("possible_sources", "sources", "retrieval_diagnostics"):
                    if key in payload:
                        payload.pop(key)
                        changed = True
                if changed:
                    message["content"] = json.dumps(payload, ensure_ascii=False)
            if changed:
                pruned_count += 1
                pruning_reasons["tool_observation_trimmed"] = \
                    pruning_reasons.get("tool_observation_trimmed", 0) + 1
            return changed

        def prune_decisions():
            nonlocal pruned_count
            items = state.get("decision_memories") or []
            if items and any("statement" in item for item in items):
                state["decision_memories"] = self._trim_memories(memories, detailed=False)
                if not state["decision_memories"]:
                    state.pop("decision_memories")
                pruned_count += 1
                pruning_reasons["decision_clues_compressed"] = \
                    pruning_reasons.get("decision_clues_compressed", 0) + 1
                return True
            return False

        def prune_coverage():
            nonlocal pruned_count
            coverage_state = state.get("task_coverage") or {}
            if coverage_state.get("authority_levels") or \
                    coverage_state.get("decision_binding"):
                state["task_coverage"] = self._trim_coverage(coverage, detailed=False)
                pruned_count += 1
                pruning_reasons["coverage_detail_trimmed"] = \
                    pruning_reasons.get("coverage_detail_trimmed", 0) + 1
                return True
            return False

        def prune_session():
            nonlocal pruned_count, user_content, trimmed_session
            if trimmed_session is None:
                return False
            payload = state.get("session_context_meta") or {}
            if payload.get("previous_investigations", 0) == 0:
                return False
            trimmed_session_new, stats = self._trim_session(
                trimmed_session, counter, max(80, session_cap // 2))
            if trimmed_session_new == trimmed_session:
                return False
            trimmed_session = trimmed_session_new
            user_content = (f"{user_question}\n\n"
                            "[会话上下文：仅用于理解追问，不是事实来源；任何结论必须由本次运行重新取得并校验Evidence]\n"
                            f"{trimmed_session or ''}\n[会话上下文结束]")
            request[1]["content"] = user_content
            state["session_context_meta"] = {
                **self._session_stats(trimmed_session), "fact_source": False,
                "note": "历史回答不是事实来源。"}
            pruned_count += 1
            pruning_reasons["session_history_compressed"] = \
                pruning_reasons.get("session_history_compressed", 0) + 1
            return True

        # Explicit priority order.  Mandatory state is never in this list.
        token_actions = [prune_session, prune_recent, prune_tool_history,
                         prune_decisions, prune_coverage]
        while over_tokens():
            changed = False
            for action in token_actions:
                if over_tokens() and action():
                    request = render(state, recent, user_content)
                    changed = True
                    break
            if over_tokens() and drop_one_excerpt("evidence_budget"):
                request = render(state, recent, user_content)
                changed = True
            if not changed:
                break
        # The byte limit remains an auxiliary hard constraint.  Excerpt bodies
        # are the first optional material to omit, but nothing mandatory is cut.
        while over_bytes():
            if drop_one_excerpt("request_context_byte_budget"):
                request = render(state, recent, user_content)
                continue
            changed = False
            for action in token_actions:
                if over_bytes() and action():
                    request = render(state, recent, user_content)
                    changed = True
                    break
            if not changed:
                break
        actual_tokens = counter.count_messages(request)
        actual_bytes = size(request)
        if actual_tokens > ceiling or actual_bytes > max_bytes:
            detail = {
                **policy.trace(), **counter.trace(),
                "context_schema_version": CONTEXT_SCHEMA_VERSION,
                "budget_policy_version": BUDGET_POLICY_VERSION,
                "estimated_input_tokens": actual_tokens,
                "input_token_ceiling": ceiling,
                "input_bytes": actual_bytes,
                "limit_bytes": max_bytes,
                "omitted_excerpt_bodies": omitted_bodies,
                "pruned_count": pruned_count,
                "pruning_reasons": dict(pruning_reasons),
                "context_limit": {"code": "context_limit",
                                  "reason": ("mandatory_state_exceeds_budget"
                                             if actual_tokens > ceiling
                                             else "mandatory_state_exceeds_byte_budget"),
                                  "category": "mandatory_state",
                                  "required_input_tokens": actual_tokens,
                                  "available_input_tokens": ceiling,
                                  "required_bytes": actual_bytes,
                                  "available_bytes": max_bytes},
            }
            error = ContextLimitError(
                "Mandatory question/index/tool state exceeds request byte budget",
                detail)
            raise error

        conflict_identities = {(item.get("evidence_id"), item.get("content_hash"))
                               for item in state["evidence_index"]}
        conflict_refs = self._conflict_keys(ledger)
        body_identities = {_identity(view) for view in state["retained_excerpts"]
                           if _view_content(view)}
        for message in recent:
            if message.get("role") != "user":
                continue
            try:
                payload = json.loads(message.get("content") or "")
            except (ValueError, TypeError):
                continue
            for view in (payload.get("evidence") if isinstance(payload, dict) else None) or []:
                if isinstance(view, dict) and _view_content(view):
                    body_identities.add(_identity(view))
        conflict_bodies = len(conflict_refs & body_identities)
        state_core = {key: value for key, value in state.items()
                      if key != "retained_excerpts"}
        category_tokens = {
            "system_rules": counter.count_text(system_content),
            "current_user_question": counter.count_text(user_question),
            "session_history": counter.count_text(trimmed_session or ""),
            "investigation_state": counter.count_text(
                json.dumps(state_core, ensure_ascii=False)),
            "evidence_excerpts": sum(counter.count_text(_view_content(view))
                                     for view in state["retained_excerpts"]),
            "tool_observations": counter.count_text(
                json.dumps(recent, ensure_ascii=False)),
            "reserved_output_tokens": policy.reserved_output_tokens,
            "safety_margin_tokens": policy.safety_margin_tokens,
        }
        usage = {
            **policy.trace(), **counter.trace(),
            "estimated_input_tokens": actual_tokens,
            "input_token_ceiling": ceiling,
            "input_bytes": actual_bytes,
            "limit_bytes": max_bytes,
            "category_tokens": category_tokens,
            "categories_estimated": not counter.exact,
            "candidate_evidence_count": selection["candidate_count"],
            "selected_evidence_count": len(state["retained_excerpts"]),
            "unique_evidence_count": selection["unique_evidence_count"],
            "deduped_evidence_count": max(
                0, selection["candidate_count"] - selection["unique_evidence_count"]),
            "deduped_excerpt_count": selection["deduped_excerpts"]
                + sum(1 for view in state["retained_excerpts"] if not _view_content(view)),
            "pruned_count": pruned_count,
            "pruning_reasons": dict(pruning_reasons),
            "important_conflicts_retained": bool(conflict_refs
                                                 and conflict_refs <= conflict_identities),
            "conflict_count": len(conflict_refs),
            "conflict_bodies_retained": conflict_bodies,
            "gap_count": len(gap_list),
            "evidence_decisions": selection["decisions"][:200],
            "omitted_excerpt_bodies": omitted_bodies,
            "unit": "utf8_bytes_not_model_tokens",
            "context_limit": None,
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "budget_policy_version": BUDGET_POLICY_VERSION,
            "model": model,
            "reproducibility": "deterministic_given_identical_inputs_and_policy",
        }
        return request, usage

    # ------------------------------------------------------------------
    # Small helpers kept private to this module
    # ------------------------------------------------------------------

    @staticmethod
    def _recent_source(messages):
        if len(messages) > 3 and messages[-2].get("role") == "assistant":
            return messages[-1]
        if len(messages) > 2:
            return messages[-1]
        return None
