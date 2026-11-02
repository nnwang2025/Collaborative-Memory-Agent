"""Locally reviewed Decision Memory. Separate SQLite; never rewrites Evidence."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import uuid4

from src.evidence import EvidenceScope, sha256_text
from src.evidence_store import EvidenceStore, default_db_path
from src.investigation import EvidenceRef, InvestigationResult


class RevisionConflict(ValueError):
    pass


class DecisionEvidence(EvidenceRef):
    relation_type: Literal["supports", "contradicts"] = "supports"


def timestamp(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(date, datetime) or date.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return date.astimezone(timezone.utc).isoformat()


def identifier(prefix):
    return prefix + "_" + uuid4().hex


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS decisions (
      decision_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, dataset_kind TEXT NOT NULL CHECK(dataset_kind IN ('fixture','live')),
      title TEXT NOT NULL, created_at TEXT NOT NULL, current_confirmed_version_id TEXT, revision INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS review_proposals (
      proposal_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions, proposed_statement TEXT NOT NULL,
      proposed_event_time TEXT, evidence_refs TEXT NOT NULL, source_run_id TEXT NOT NULL,
      review_status TEXT NOT NULL CHECK(review_status IN ('pending','accepted','rejected')), created_at TEXT NOT NULL,
      reviewed_at TEXT, reviewer_id TEXT, review_note TEXT, risk_flags TEXT NOT NULL, base_revision INTEGER NOT NULL,
      dedupe_key TEXT NOT NULL UNIQUE, investigation_context TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS decision_versions (
      version_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions, revision INTEGER NOT NULL,
      statement TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','superseded','retracted','unknown')),
      event_time TEXT, recorded_at TEXT NOT NULL, supersedes_version_id TEXT REFERENCES decision_versions,
      reviewer_id TEXT NOT NULL, reviewed_at TEXT NOT NULL, proposal_id TEXT NOT NULL UNIQUE REFERENCES review_proposals,
      UNIQUE(decision_id,revision))""",
    """CREATE TABLE IF NOT EXISTS proposal_observations (
      proposal_id TEXT NOT NULL REFERENCES review_proposals, observation_key TEXT NOT NULL,
      source_run_id TEXT NOT NULL, context_json TEXT NOT NULL, risk_flags TEXT NOT NULL, recorded_at TEXT NOT NULL,
      PRIMARY KEY(proposal_id,observation_key))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_version ON decision_versions(decision_id) WHERE status='confirmed'""",
    """CREATE TABLE IF NOT EXISTS decision_evidence (
      version_id TEXT NOT NULL REFERENCES decision_versions, evidence_id TEXT NOT NULL, content_hash TEXT NOT NULL,
      relation_type TEXT NOT NULL CHECK(relation_type IN ('supports','contradicts')),
      PRIMARY KEY(version_id,evidence_id,content_hash,relation_type))""",
    """CREATE TABLE IF NOT EXISTS conflict_links (
      conflict_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions,
      left_version_id TEXT REFERENCES decision_versions, right_version_id TEXT REFERENCES decision_versions,
      left_proposal_id TEXT REFERENCES review_proposals, right_proposal_id TEXT REFERENCES review_proposals,
      conflict_description TEXT NOT NULL, resolution_status TEXT NOT NULL DEFAULT 'open',
      reviewer_id TEXT, reviewed_at TEXT, review_note TEXT, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS decision_review_history (
      seq INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES review_proposals, decision_id TEXT NOT NULL REFERENCES decisions,
      operation TEXT NOT NULL, before_revision INTEGER NOT NULL, after_revision INTEGER NOT NULL,
      before_version_id TEXT, after_version_id TEXT, reviewer_id TEXT NOT NULL, reviewed_at TEXT NOT NULL, review_note TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS immutable_version_body BEFORE UPDATE OF statement,event_time,recorded_at,decision_id,
      revision,supersedes_version_id,reviewer_id,reviewed_at,proposal_id ON decision_versions
      BEGIN SELECT RAISE(ABORT,'Version bodies and provenance are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_delete_versions BEFORE DELETE ON decision_versions
      BEGIN SELECT RAISE(ABORT,'Versions cannot be deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_proposal_body BEFORE UPDATE OF decision_id,proposed_statement,
      proposed_event_time,evidence_refs,source_run_id,created_at,risk_flags,base_revision,dedupe_key,investigation_context ON review_proposals
      BEGIN SELECT RAISE(ABORT,'Proposal bodies are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS immutable_decision_evidence BEFORE UPDATE ON decision_evidence
      BEGIN SELECT RAISE(ABORT,'Evidence links are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_delete_decision_evidence BEFORE DELETE ON decision_evidence
      BEGIN SELECT RAISE(ABORT,'Evidence links cannot be deleted'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_update_review_history BEFORE UPDATE ON decision_review_history
      BEGIN SELECT RAISE(ABORT,'Review history is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS no_delete_review_history BEFORE DELETE ON decision_review_history
      BEGIN SELECT RAISE(ABORT,'Review history cannot be deleted'); END""",
]


class DecisionStore:
    def __init__(self, path=None, *, evidence_db_path=None, clock=None):
        evidence_path = Path(evidence_db_path) if evidence_db_path is not None else default_db_path()
        self.path = Path(path) if path is not None else Path(os.getenv("DECISION_DB_PATH", str(evidence_path.with_name("decisions.sqlite3"))))
        if self.path.resolve() == evidence_path.resolve():
            raise ValueError("Decision Memory must use a separate database; no Evidence schema migration")
        self.evidence = EvidenceStore(evidence_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for statement in SCHEMA:
                db.execute(statement)
            yield db

    @contextmanager
    def _read(self):
        if not self.path.exists():
            yield None
        else:
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                db.execute("BEGIN")  # consistent topic, pointer and history snapshot
                yield db

    @staticmethod
    def _topic(db, decision_id, scope):
        if db is None:
            raise KeyError("Decision not found in scope")
        topic = db.execute("SELECT * FROM decisions WHERE decision_id=? AND project_id=? AND dataset_kind=?",
                           (decision_id, scope.project_id, scope.dataset_kind)).fetchone()
        if topic is None:
            raise KeyError("Decision not found in scope")
        return dict(topic)

    def create(self, title, scope):
        scope = EvidenceScope.model_validate(scope)
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise ValueError("Decision title must contain 1-300 characters")
        decision_id = identifier("dec")
        with self._write() as db:
            db.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?)",
                       (decision_id, scope.project_id, scope.dataset_kind, title.strip(), timestamp(self.clock()), None, 0))
            return self._topic(db, decision_id, scope)

    def _validate_refs(self, scope, refs):
        validated = []
        for value in refs:
            ref = DecisionEvidence.model_validate(value.model_dump() if isinstance(value, DecisionEvidence) else value)
            evidence = self.evidence.get(ref.evidence_id, scope, ref.content_hash)
            if evidence is None:
                raise ValueError("Evidence reference is missing, out of scope, or has wrong hash")
            validated.append((ref, evidence))
        if not validated or not any(r.relation_type == "supports" for r, _ in validated):
            raise ValueError("A proposal requires supporting Evidence")
        return validated

    def propose(self, decision_id, result, statement, *, refs=None, event_time=None):
        """Explicit local proposal submission. An InvestigationResult is input, not authorization."""
        result = InvestigationResult.model_validate(result.model_dump() if isinstance(result, InvestigationResult) else result)
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 4000:
            raise ValueError("Proposal statement must contain 1-4000 characters")
        event = timestamp(event_time) if event_time is not None else None
        values = refs if refs is not None else [r.model_dump() for r in result.evidence_refs]
        checked = self._validate_refs(result.scope, values)
        actual = {(r.evidence_id, r.content_hash) for r in result.evidence_refs}
        if any((r.evidence_id, r.content_hash) not in actual for r, _ in checked):
            raise ValueError("Proposal references were not obtained in the submitted investigation")
        refs_data = sorted({json.dumps(r.model_dump(), sort_keys=True) for r, _ in checked})
        normalized_refs = [json.loads(r) for r in refs_data]
        risks = ["identity_only_not_semantically_verified"]
        if result.gaps: risks.append("investigation_gaps")
        if result.conflicts: risks.append("investigation_conflicts")
        if result.termination_reason != "sufficient": risks.append("investigation_incomplete")
        if any(e.metadata.get("legacy") for _, e in checked): risks.append("unverified_legacy")
        if any(r.relation_type == "contradicts" for r, _ in checked): risks.append("contradicting_evidence")
        fingerprint = sha256_text(json.dumps([decision_id, statement.strip(), event, normalized_refs], sort_keys=True, ensure_ascii=False))
        context = {"termination_reason": result.termination_reason, "validation_level": result.validation_level,
                   "gaps": result.gaps, "conflicts": [c.model_dump() for c in result.conflicts]}
        with self._write() as db:
            topic = self._topic(db, decision_id, result.scope)
            existing = db.execute("SELECT proposal_id FROM review_proposals WHERE dedupe_key=?", (fingerprint,)).fetchone()
            if existing:
                self._observe_proposal(db, existing[0], result.run_id, context, risks)
                return self._proposal(db, existing[0], result.scope)
            proposal_id = identifier("prop")
            db.execute("""INSERT INTO review_proposals(proposal_id,decision_id,proposed_statement,proposed_event_time,
                evidence_refs,source_run_id,review_status,created_at,risk_flags,base_revision,dedupe_key,investigation_context)
                VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?)""", (proposal_id, decision_id, statement.strip(), event,
                json.dumps(normalized_refs), result.run_id, timestamp(self.clock()), json.dumps(risks), topic["revision"], fingerprint, json.dumps(context)))
            self._observe_proposal(db, proposal_id, result.run_id, context, risks)
            if topic["current_confirmed_version_id"]:
                old = db.execute("SELECT statement FROM decision_versions WHERE version_id=?", (topic["current_confirmed_version_id"],)).fetchone()
                if old[0] != statement.strip():
                    db.execute("INSERT INTO conflict_links(conflict_id,decision_id,left_version_id,right_proposal_id,conflict_description,created_at) VALUES (?,?,?,?,?,?)",
                        (identifier("conf"), decision_id, topic["current_confirmed_version_id"], proposal_id, "候选与当前确认正文不同，需人工判断变化关系。", timestamp(self.clock())))
            for other in db.execute("SELECT proposal_id FROM review_proposals WHERE decision_id=? AND proposal_id<>? AND review_status='pending' AND proposed_statement<>?",
                                    (decision_id, proposal_id, statement.strip())).fetchall():
                db.execute("INSERT INTO conflict_links(conflict_id,decision_id,left_proposal_id,right_proposal_id,conflict_description,created_at) VALUES (?,?,?,?,?,?)",
                    (identifier("conf"), decision_id, other[0], proposal_id, "不同候选正文存在差异，未自动判定哪一方正确。", timestamp(self.clock())))
            return self._proposal(db, proposal_id, result.scope)

    def _observe_proposal(self, db, proposal_id, run_id, context, risks):
        key = sha256_text(json.dumps([run_id, context, risks], sort_keys=True, ensure_ascii=False))
        db.execute("INSERT INTO proposal_observations VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                   (proposal_id, key, run_id, json.dumps(context), json.dumps(risks), timestamp(self.clock())))

    def _proposal(self, db, proposal_id, scope, known_at=None):
        row = db.execute("SELECT p.* FROM review_proposals p JOIN decisions d ON p.decision_id=d.decision_id WHERE proposal_id=? AND project_id=? AND dataset_kind=?",
                         (proposal_id, scope.project_id, scope.dataset_kind)).fetchone() if db else None
        if row is None: raise KeyError("Proposal not found in scope")
        value = dict(row)
        for key in ("evidence_refs", "risk_flags", "investigation_context"):
            value[key] = json.loads(value[key])
        observations = [dict(r) for r in db.execute("SELECT * FROM proposal_observations WHERE proposal_id=? ORDER BY recorded_at,observation_key", (proposal_id,)) if not known_at or r["recorded_at"] <= known_at]
        for observation in observations:
            observation["context_json"] = json.loads(observation["context_json"])
            observation["risk_flags"] = json.loads(observation["risk_flags"])
        value["investigation_observations"] = observations
        value["risk_flags"] = sorted(set(value["risk_flags"]) | {risk for o in observations for risk in o["risk_flags"]})
        return value

    def proposal(self, proposal_id, scope):
        with self._read() as db:
            return self._proposal(db, proposal_id, scope)

    def review(self, proposal_id, scope, *, operation, reviewer_id, expected_revision, note, acknowledge_risks=False):
        """TRUSTED LOCAL ONLY. reviewer_id is an audit label, never HTTP authentication."""
        if operation not in ("accept", "reject") or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("Invalid review operation or expected revision")
        if not isinstance(reviewer_id, str) or not reviewer_id.strip() or len(reviewer_id) > 200:
            raise ValueError("A local reviewer audit label is required")
        if not isinstance(note, str) or not note.strip() or len(note) > 2000:
            raise ValueError("A review note is required")
        now = timestamp(self.clock())
        with self._write() as db:
            proposal = self._proposal(db, proposal_id, scope)
            topic = self._topic(db, proposal["decision_id"], scope)
            if topic["revision"] != expected_revision:
                raise RevisionConflict("Decision revision changed; inspect current state and review again")
            if proposal["review_status"] != "pending":
                raise RevisionConflict("Proposal already reviewed")
            old_id = topic["current_confirmed_version_id"]
            new_id = old_id
            if operation == "accept":
                checked = self._validate_refs(scope, proposal["evidence_refs"])
                if not any(r.relation_type == "supports" and not e.metadata.get("legacy") for r, e in checked):
                    raise ValueError("Unverified legacy Evidence cannot solely confirm a decision")
                if proposal["risk_flags"] and not acknowledge_risks:
                    raise ValueError("Reviewer must explicitly acknowledge the proposal risks")
                new_id = identifier("dv")
                if old_id:
                    db.execute("UPDATE decision_versions SET status='superseded' WHERE version_id=? AND status='confirmed'", (old_id,))
                db.execute("INSERT INTO decision_versions VALUES (?,?,?,?,'confirmed',?,?,?,?,?,?)",
                    (new_id, topic["decision_id"], expected_revision + 1, proposal["proposed_statement"], proposal["proposed_event_time"],
                     now, old_id, reviewer_id.strip(), now, proposal_id))
                for relation in proposal["evidence_refs"]:
                    db.execute("INSERT INTO decision_evidence VALUES (?,?,?,?)",
                        (new_id, relation["evidence_id"], relation["content_hash"], relation["relation_type"]))
            db.execute("UPDATE review_proposals SET review_status=?,reviewed_at=?,reviewer_id=?,review_note=? WHERE proposal_id=?",
                ("accepted" if operation == "accept" else "rejected", now, reviewer_id.strip(), note.strip(), proposal_id))
            cursor = db.execute("UPDATE decisions SET current_confirmed_version_id=?,revision=revision+1 WHERE decision_id=? AND revision=?",
                (new_id, topic["decision_id"], expected_revision))
            if cursor.rowcount != 1: raise RevisionConflict("Revision changed")
            db.execute("INSERT INTO decision_review_history(proposal_id,decision_id,operation,before_revision,after_revision,before_version_id,after_version_id,reviewer_id,reviewed_at,review_note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, topic["decision_id"], operation, expected_revision, expected_revision + 1, old_id, new_id, reviewer_id.strip(), now, note.strip()))
            for link in db.execute("SELECT * FROM conflict_links WHERE (left_proposal_id=? OR right_proposal_id=?) AND resolution_status='open'", (proposal_id, proposal_id)).fetchall():
                other = link["right_proposal_id"] if link["left_proposal_id"] == proposal_id else link["left_proposal_id"]
                other_pending = other and db.execute("SELECT review_status FROM review_proposals WHERE proposal_id=?", (other,)).fetchone()[0] == "pending"
                status = "open" if operation == "accept" and other_pending else "reviewed_acceptance" if operation == "accept" else "candidate_rejected"
                db.execute("UPDATE conflict_links SET resolution_status=?,left_version_id=CASE WHEN left_proposal_id=? THEN ? ELSE left_version_id END,right_version_id=CASE WHEN right_proposal_id=? THEN ? ELSE right_version_id END,reviewer_id=?,reviewed_at=?,review_note=? WHERE conflict_id=?",
                    (status, proposal_id, new_id if operation == "accept" else None, proposal_id, new_id if operation == "accept" else None,
                     reviewer_id.strip(), now, note.strip(), link["conflict_id"]))
            return self._topic(db, topic["decision_id"], scope)

    def snapshot(self, decision_id, scope, *, known_at=None):
        cutoff = timestamp(known_at) if known_at is not None else None
        with self._read() as db:
            topic = self._topic(db, decision_id, scope)
            if cutoff and topic["created_at"] > cutoff:
                return {"decision": None, "current_confirmed": None, "versions": [], "timeline": [],
                        "proposals": [], "review_history": [], "conflicts": [], "known_at": cutoff,
                        "world_state": "unknown", "time_view": "system_knowledge"}
            rows = db.execute("SELECT * FROM decision_versions WHERE decision_id=? ORDER BY revision", (decision_id,)).fetchall()
            versions = []
            for row in rows:
                version = dict(row)
                version["evidence_refs"] = [dict(r) for r in db.execute("SELECT evidence_id,content_hash,relation_type FROM decision_evidence WHERE version_id=?", (version["version_id"],))]
                if not cutoff or version["recorded_at"] <= cutoff:
                    versions.append(version)
            history = [dict(r) for r in db.execute("SELECT * FROM decision_review_history WHERE decision_id=? ORDER BY seq", (decision_id,)) if not cutoff or r["reviewed_at"] <= cutoff]
            current_id = history[-1]["after_version_id"] if cutoff and history else None if cutoff else topic["current_confirmed_version_id"]
            if cutoff:
                for version in versions:
                    version["status"] = "confirmed" if version["version_id"] == current_id else "superseded"
                topic["current_confirmed_version_id"] = current_id
                topic["revision"] = history[-1]["after_revision"] if history else 0
            proposals = [self._proposal(db, r[0], scope, cutoff) for r in db.execute("SELECT proposal_id FROM review_proposals WHERE decision_id=?", (decision_id,))]
            if cutoff:
                proposals = [p for p in proposals if p["created_at"] <= cutoff]
                for proposal in proposals:
                    if proposal["reviewed_at"] and proposal["reviewed_at"] > cutoff:
                        proposal.update(review_status="pending", reviewed_at=None, reviewer_id=None, review_note=None)
            conflicts = [dict(r) for r in db.execute("SELECT * FROM conflict_links WHERE decision_id=?", (decision_id,)) if not cutoff or r["created_at"] <= cutoff]
            if cutoff:
                for conflict in conflicts:
                    if conflict["reviewed_at"] and conflict["reviewed_at"] > cutoff:
                        conflict.update(resolution_status="open", reviewer_id=None, reviewed_at=None, review_note=None, right_version_id=None)
            timeline = sorted(({
                "kind": "decision_version",
                "version_id": version["version_id"],
                "revision": version["revision"],
                "status": version["status"],
                "statement": version["statement"],
                "event_time": version["event_time"],
                "event_time_status": "recorded" if version["event_time"] else "unknown",
                "decision_known_at": version["reviewed_at"],
                "recorded_at": version["recorded_at"],
                "supersedes_version_id": version["supersedes_version_id"],
                "evidence_refs": version["evidence_refs"],
            } for version in versions), key=lambda item: (
                item["event_time"] is None,
                item["event_time"] or "",
                item["revision"],
            ))
            return {"decision": topic, "current_confirmed": next((v for v in versions if v["version_id"] == current_id), None),
                    "versions": versions, "timeline": timeline,
                    "proposals": proposals, "review_history": history, "conflicts": conflicts,
                    "known_at": cutoff, "world_state": "unknown", "time_view": "system_knowledge"}

    def topics(self, scope):
        """Read-only topic listing for trusted local demos and explicit IDs."""
        with self._read() as db:
            if db is None:
                return []
            return [dict(r) for r in db.execute(
                "SELECT * FROM decisions WHERE project_id=? AND dataset_kind=? ORDER BY created_at,decision_id",
                (scope.project_id, scope.dataset_kind))]

    @staticmethod
    def _event_time(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None

    @classmethod
    def _version_in_time_range(cls, version, after=None, before=None):
        event = cls._event_time(version.get("event_time"))
        if after is not None or before is not None:
            if event is None:
                return False
            if after is not None and event < after:
                return False
            if before is not None and event > before:
                return False
        return True

    @staticmethod
    def _timeline_key(version):
        event = DecisionStore._event_time(version.get("event_time"))
        # Unknown event time is never guessed from recorded_at/reviewed_at.
        return (event is None, event or datetime.max.replace(tzinfo=timezone.utc), version["revision"])

    def _evidence_details(self, scope, refs):
        details = []
        for ref in refs:
            record = self.evidence.get(ref["evidence_id"], scope, ref["content_hash"])
            details.append({
                **ref,
                "source_type": record.source_type if record else None,
                "source_uri": record.source_uri if record else None,
                "original_record_id": record.original_record_id if record else None,
                "event_time": record.event_time.isoformat() if record and record.event_time else None,
                "ingested_at": record.ingested_at.isoformat() if record and record.ingested_at else None,
                "provenance_status": "verified" if record else "unresolved",
            })
        return details

    def _topic_documents(self, db, scope, cutoff=None):
        """Body-aware ranking input: title plus versions known at the cutoff."""
        docs = []
        for row in db.execute("SELECT * FROM decisions WHERE project_id=? AND dataset_kind=?",
                              (scope.project_id, scope.dataset_kind)):
            topic = dict(row)
            versions = db.execute("SELECT statement,reviewed_at FROM decision_versions "
                                  "WHERE decision_id=? ORDER BY revision", (topic["decision_id"],))
            statements = [v["statement"] for v in versions if not cutoff or v["reviewed_at"] <= cutoff]
            docs.append({**topic, "content": "\n".join([topic["title"], *statements])})
        return docs

    def search(self, query, scope, *, decision_id=None, view="current", known_at=None, top_k=5,
               after=None, before=None):
        """Search reviewed decision titles and version bodies, with time boundaries.

        ``known_at`` selects the system-knowledge snapshot. ``after``/``before``
        filter the decision's own event_time; an unknown event time is excluded
        rather than inferred from recorded_at or ingested time.
        """
        if view not in ("current", "history"):
            raise ValueError("view must be current or history")
        cutoff = timestamp(known_at) if known_at is not None else None
        after = self._event_time(timestamp(after)) if after is not None else None
        before = self._event_time(timestamp(before)) if before is not None else None
        with self._read() as db:
            if db is None:
                return []
            from src.skills.utils import bm25_rank
            topics = [dict(r) for r in db.execute(
                "SELECT * FROM decisions WHERE project_id=? AND dataset_kind=?",
                (scope.project_id, scope.dataset_kind))]
            if decision_id:
                topics = [t for t in topics if t["decision_id"] == decision_id]
            else:
                topics = bm25_rank(query, self._topic_documents(db, scope, cutoff))
        results = []
        for topic in topics:
            snapshot = self.snapshot(topic["decision_id"], scope, known_at=cutoff)
            if snapshot["decision"] is None:
                continue
            versions = snapshot["versions"] if view == "history" else (
                [snapshot["current_confirmed"]] if snapshot["current_confirmed"] else [])
            versions = [v for v in versions if self._version_in_time_range(v, after, before)]
            if view == "history" and versions:
                # Rank version bodies for the query, then keep chronology for ties.
                version_docs = [{"content": topic["title"] + "\n" + version["statement"],
                                 "version_id": version["version_id"]} for version in versions]
                scores = {item["version_id"]: item.get("score", 0)
                          for item in bm25_rank(query, version_docs)}
                versions.sort(key=lambda version: (
                    -scores.get(version["version_id"], 0), self._timeline_key(version)))
            elif view == "history":
                versions.sort(key=self._timeline_key)
            for version in versions:
                results.append({
                    "record_kind": "decision_version",
                    **version,
                    "project_id": scope.project_id,
                    "dataset_kind": scope.dataset_kind,
                    "title": topic["title"],
                    "authority": "reviewed_local_decision",
                    "decision_known_at": version["reviewed_at"],
                    "knowledge_cutoff": snapshot["known_at"],
                    "time_view": "system_knowledge",
                    "event_time_status": "recorded" if version.get("event_time") else "unknown",
                    "execution_status": "unknown",
                    "system_current": version["version_id"] == (snapshot["current_confirmed"] or {}).get("version_id"),
                    "world_state": "unknown",
                    "evidence": self._evidence_details(scope, version["evidence_refs"]),
                    "decision_conflicts": [{
                        "conflict_id": item["conflict_id"],
                        "resolution_status": item["resolution_status"],
                        "conflict_description": item["conflict_description"],
                    } for item in snapshot.get("conflicts", [])],
                })
                if len(results) >= top_k:
                    return results
        return results
