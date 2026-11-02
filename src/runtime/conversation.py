"""Bounded session context for a follow-up investigation.

Historical answers are hints, not facts.  Only EvidenceRefs that still resolve
to an exact stored Evidence version are carried forward, and the Agent must
retrieve and validate evidence again before any claim can cite it.
"""

from __future__ import annotations

import json

from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore
from src.runtime.store import RuntimeStore


class ConversationContextBuilder:
    def __init__(self, store: RuntimeStore, *, max_runs=3, max_bytes=6000,
                 max_refs=10, evidence_store=None):
        self.store = store
        self.max_runs = max(0, int(max_runs))
        self.max_bytes = max(500, int(max_bytes))
        self.max_refs = max(0, int(max_refs))
        self.evidence = evidence_store or EvidenceStore(store.path)

    def _verified_refs(self, run_id, scope, limit):
        refs = []
        for row in self.store.list_evidence_refs(run_id):
            if len(refs) >= limit:
                break
            record = self.evidence.get(
                row["evidence_id"], scope, row["content_hash"])
            if record is None:
                continue
            refs.append({
                "evidence_id": record.evidence_id,
                "content_hash": record.content_hash,
                "source_type": record.source_type,
                "original_record_id": record.original_record_id,
                "event_time": record.event_time.isoformat() if record.event_time else None,
            })
        return refs

    @staticmethod
    def _short(value, limit):
        text = str(value or "").strip()
        return text if len(text) <= limit else text[:limit] + "…"

    def build(self, session_id: str, *, current_run_id=None) -> str | None:
        session = self.store.get_session(session_id)
        if session is None or not self.max_runs:
            return None
        scope = EvidenceScope(project_id=session["project_id"],
                              dataset_kind=session["dataset_kind"])
        runs = [run for run in self.store.list_runs(
            session_id, limit=20) if run["run_id"] != current_run_id]
        runs = [run for run in runs if run["status"] not in {"queued", "running"}]
        runs = runs[: self.max_runs]
        if not runs:
            return None
        remaining_refs = self.max_refs
        history = []
        for run in reversed(runs):  # oldest first for natural reading
            result = self.store.get_result(run["run_id"])
            payload = (result or {}).get("result") or {}
            refs = self._verified_refs(run["run_id"], scope, remaining_refs)
            remaining_refs -= len(refs)
            decisions = []
            for memory in payload.get("decision_memories", [])[:3]:
                if not isinstance(memory, dict):
                    continue
                decisions.append({
                    "decision_id": memory.get("decision_id"),
                    "version_id": memory.get("version_id"),
                    "title": self._short(memory.get("title"), 160),
                    "system_current": memory.get("system_current"),
                })
            history.append({
                "run_id": run["run_id"],
                "question": self._short(run["question"], 500),
                "status": run["status"],
                "termination_reason": run["termination_reason"],
                "answer_hint": self._short((result or {}).get("answer"), 700),
                "unresolved_gaps": [self._short(gap, 240)
                                    for gap in (payload.get("gaps") or [])[:5]],
                "conflicts": [self._short(item.get("description"), 240)
                              for item in (payload.get("conflicts") or [])[:3]
                              if isinstance(item, dict)],
                "verified_evidence_refs": refs,
                "decision_clues": decisions,
            })
        context = {
            "note": "历史回答不是事实来源；仅可用来理解追问。引用必须由本次运行重新取得并校验。",
            "session_id": session_id,
            "previous_investigations": history,
        }
        rendered = json.dumps(context, ensure_ascii=False)
        while len(rendered.encode("utf-8")) > self.max_bytes and history:
            history.pop(0)
            context["previous_investigations"] = history
            rendered = json.dumps(context, ensure_ascii=False)
        if not history:
            return None
        return rendered
