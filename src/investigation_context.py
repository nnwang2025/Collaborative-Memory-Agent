"""Deterministic live evidence views and request budgeting; no model or storage writes."""
import copy
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.evidence import Evidence


class Excerpt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    evidence_id: str
    content_hash: str
    project_id: str
    dataset_kind: Literal["live", "fixture"]
    text: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    offset_unit: Literal["unicode_codepoint"] = "unicode_codepoint"

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
    "initial_reason": r"原因|理由|因为|由于|因此|所以|为了|以便|避免|防止|不足|风险|约束|代价|背景|需求|动机|because|due to|reason|so that|to avoid|root cause",
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
# Topic words that cannot, on their own, bind a reviewed Decision Memory record
# to the user's question. Project names are added dynamically per ledger.
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
    pass


class InvestigationContext:
    """Retain identities/locators/tool state; at most one copy of each excerpt per request."""
    def __init__(self):
        self.views = {}

    @staticmethod
    def key(view):
        excerpt = view.get("excerpt", {})
        return (view["evidence_id"], view["content_hash"], excerpt.get("char_start"), excerpt.get("char_end"))

    def observe(self, items):
        result = []
        for item in items:
            parent = Evidence.model_validate({k: item[k] for k in Evidence.model_fields if k in item})
            snippet = Excerpt.model_validate(item["excerpt"]).verify(parent)
            view = {"evidence_id": parent.evidence_id, "content_hash": parent.content_hash,
                "project_id": parent.project_id, "dataset_kind": parent.dataset_kind, "source_type": parent.source_type,
                "source_uri": parent.source_uri, "event_time": item.get("event_time"), "locator": parent.locator,
                "time_status": "unknown" if parent.event_time is None else "recorded",
                "legacy_unverified": bool(parent.metadata.get("legacy")),
                "excerpt": snippet.model_dump(exclude={"text"}), "content": snippet.text}
            key = self.key(view)
            if key in self.views:
                result.append({**view, "content": "", "body_omitted": "already_in_evidence_index"})
            else:
                self.views[key] = view
                result.append(view)
        return result

    def request(self, messages, ledger, trace, memories, coverage, max_bytes, gaps=()):
        recent = copy.deepcopy(messages[-2:] if len(messages) > 3 and messages[-2]["role"] == "assistant" else messages[2:][-1:])
        latest_views = []
        if recent:
            try: latest_views = json.loads(recent[-1]["content"]).get("evidence", [])
            except (ValueError, AttributeError): pass
        latest_keys = {self.key(v) for v in latest_views if v.get("content")}
        earlier = [copy.deepcopy(v) for k, v in self.views.items() if k not in latest_keys]
        index = []
        for entry in ledger.entries:
            parent = entry.evidence
            index.append({**parent.model_dump(mode="json", exclude={"content", "metadata", "ingested_at"}),
                "time_status": "unknown" if parent.event_time is None else "recorded",
                "excerpts": [v["excerpt"] for k, v in self.views.items() if k[:2] == (parent.evidence_id, parent.content_hash)]})
        state = {"evidence_index": index, "retained_excerpts": earlier, "task_coverage": coverage,
            "gaps": list(dict.fromkeys(list(ledger.gaps) + list(gaps))),
            "conflicts": [conflict.model_dump() for conflict in ledger.conflicts],
            "decision_memories": [] if latest_views else list(memories.values()),
            "tool_history": [{"tool": s.tool_name, "query": s.query, "filters": s.filters, "evidence_ids": s.evidence_ids,
                "errors": [e.model_dump() for e in s.errors], "recovery": s.recovery_kind}
                for s in trace if s.tool_name or s.errors]}
        def render():
            return messages[:2] + [{"role": "user", "content": json.dumps(state, ensure_ascii=False)}] + recent
        def size(value): return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        omitted = 0
        request = render()
        # Drop whole optional bodies, never cut a citation, locator, original question or tool error.
        for view in earlier + latest_views:
            if size(request) <= max_bytes: break
            if view.get("content"):
                view["content"] = ""
                view["body_omitted"] = "request_context_byte_budget"
                omitted += 1
            if latest_views:
                payload = json.loads(recent[-1]["content"])
                payload["evidence"] = latest_views
                recent[-1]["content"] = json.dumps(payload, ensure_ascii=False)
            request = render()
        if size(request) > max_bytes:
            raise ContextLimitError("Mandatory question/index/tool state exceeds request byte budget")
        return request, {"input_bytes": size(request), "limit_bytes": max_bytes, "omitted_excerpt_bodies": omitted,
                         "unit": "utf8_bytes_not_model_tokens"}
