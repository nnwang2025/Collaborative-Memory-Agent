from src.skills.dependency_skill import DependencySkill
from src.skills.document_skill import DocumentSkill
from src.skills.entrypoint_skill import EntrypointSkill
from src.skills.memory_skill import MemorySkill
from src.skills.problem_skill import ProblemSkill
from src.evidence import DEMO_PROJECT, EvidenceScope, legacy_evidence


class ScopedSkill:
    """Small compatibility adapter: legacy fixture skills or partitioned live evidence."""

    SOURCE_TYPES = {
        "memory": None,  # history investigations can start from any imported source
        "problem": {"git_commit", "issue", "log"},
        "document": {"document", "meeting"},
        "dependency": {"code"},
        "entrypoint": {"code", "document"},
    }

    def __init__(self, name, legacy, registry):
        self.name, self.legacy, self.registry = name, legacy, registry

    def run(self, question, top_k=5, filters=None):
        registry = self.registry
        if registry.scope.dataset_kind == "live":
            from src.retrieval import retrieve_evidence
            evidence = retrieve_evidence(question, scope=registry.scope, db_path=registry.db_path,
                                         live_dir=registry.live_dir, source_types=self.SOURCE_TYPES[self.name], top_k=top_k, filters=filters)
            result = {"answer": "已检索指定项目的真实证据。" if evidence else "当前范围内没有匹配证据。",
                      "evidence": evidence, "sources": sorted({e["source"] for e in evidence}), "entities": []}
        elif registry.scope.project_id == DEMO_PROJECT:
            result = self.legacy.run(question, top_k=top_k)
            if filters:
                from src.retrieval import filter_evidence_hits
                result = {**result, "evidence": filter_evidence_hits(result["evidence"], filters)}
        else:
            result = {"answer": "当前范围内没有演示数据。", "evidence": [], "sources": [], "entities": []}
        if self.name == "memory":
            from src.investigation import Filters
            from src.retrieval import get_evidence, filter_evidence_hits
            memories = self.legacy.query_decisions(question, registry.scope, evidence_db_path=registry.db_path,
                                                   filters=filters, top_k=top_k)
            # The decision's own event time was filtered by DecisionStore. Its
            # supporting Evidence may predate that event, so only source/identity
            # filters are re-applied here, never the decision time window.
            memory_filters = None
            if filters:
                memory_filters = filters if isinstance(filters, Filters) else Filters.model_validate(filters)
                memory_filters = memory_filters.model_copy(update={"after": None, "before": None, "undated_only": False})
            linked = []
            valid_memories = []
            for memory in memories:
                evidence = [get_evidence(ref["evidence_id"], scope=registry.scope, db_path=registry.db_path,
                    live_dir=registry.live_dir, content_hash=ref["content_hash"]) for ref in memory["evidence_refs"]]
                if any(e is None for e in evidence):
                    continue  # do not present a version whose provenance cannot be fetched
                hits = [e.as_hit() for e in evidence]
                if memory_filters and len(filter_evidence_hits(hits, memory_filters)) != len(hits):
                    continue
                valid_memories.append(memory)
                linked.extend(hits)
            by_identity = {(h["evidence_id"], h["content_hash"]): h for h in linked}
            for item in result["evidence"]:
                by_identity.setdefault((item["evidence_id"], item["content_hash"]), item)
            result = {**result, "evidence": list(by_identity.values()), "decision_memories": valid_memories}
        normalized = []
        for item in result["evidence"]:
            evidence = legacy_evidence(item)
            if (evidence.project_id, evidence.dataset_kind) != (registry.scope.project_id, registry.scope.dataset_kind):
                raise ValueError("Skill returned evidence outside query scope")
            registry._observed[(evidence.evidence_id, evidence.content_hash)] = evidence
            normalized.append(evidence.as_hit(item.get("score", 0), item.get("source")))
        return {**result, "evidence": normalized, "sources": sorted({item["source"] for item in normalized})}


class SkillRegistry:
    def __init__(self, scope=None, db_path=None, live_dir=None):
        self.scope = EvidenceScope.model_validate(scope or {})
        self.db_path, self.live_dir = db_path, live_dir
        self._observed = {}
        legacy = {
            "memory": MemorySkill(),
            "problem": ProblemSkill(),
            "dependency": DependencySkill(),
            "entrypoint": EntrypointSkill(),
            "document": DocumentSkill(),
        }
        self.skills = {name: ScopedSkill(name, skill, self) for name, skill in legacy.items()}

    def get(self, name):
        return self.skills[name]

    def get_evidence(self, evidence_id, content_hash=None):
        for (identifier, digest), record in reversed(list(self._observed.items())):
            if identifier == evidence_id and (content_hash is None or digest == content_hash):
                return record
        from src.retrieval import get_evidence
        return get_evidence(evidence_id, scope=self.scope, db_path=self.db_path,
                            live_dir=self.live_dir, content_hash=content_hash)
