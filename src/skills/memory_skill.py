from src.skills.utils import ROOT, bm25_rank, entity_score, extract_entities, format_skill_result, flatten_text, keyword_score, read_json_records


class MemorySkill:
    """Search historical discussions, semantic memories and decision memories."""

    DECISION_QUERY_SIGNALS = ["为什么", "原因", "采用", "决策", "方案", "技术选型", "没有采用"]

    def __init__(self):
        self.decision_path = ROOT / "data" / "decision_memory.json"
        self.paths = [
            ROOT / "data" / "memory_records.json",
            ROOT / "data" / "episodic_summaries.json",
            ROOT / "data" / "chats",
        ]

    def _decision_content(self, record):
        reason = "；".join(record.get("reason", []))
        alternatives = "、".join(record.get("alternatives", []))
        entities = "、".join(record.get("related_entities", []))
        sources = "、".join(record.get("source_files", []))
        return (
            f"title: {record.get('title', '')} | "
            f"decision: {record.get('decision', '')} | "
            f"alternatives: {alternatives} | "
            f"reason: {reason} | "
            f"future_plan: {record.get('future_plan', '')} | "
            f"related_entities: {entities} | "
            f"source_files: {sources}"
        )

    def _load_decision_docs(self):
        docs = []
        for index, record in enumerate(read_json_records(self.decision_path), start=1):
            docs.append({
                "source": "decision_memory",
                "record": record.get("id", index),
                "content": self._decision_content(record),
                "metadata": record,
                "memory_type": "decision",
            })
        return docs

    def _load_regular_docs(self):
        docs = []
        for path in self.paths:
            files = sorted(path.glob("*.json")) if path.is_dir() else [path]
            for file_path in files:
                for index, record in enumerate(read_json_records(file_path), start=1):
                    content = record.get("summary") or record.get("content") or flatten_text(record)
                    docs.append({
                        "source": file_path.relative_to(ROOT).as_posix(),
                        "record": record.get("record_id", index),
                        "content": content,
                        "metadata": record,
                        "memory_type": "regular",
                    })
        return docs

    def _is_decision_query(self, question):
        q = question or ""
        return any(signal in q for signal in self.DECISION_QUERY_SIGNALS)

    def _is_broad_decision_query(self, question):
        q = question or ""
        return self._is_decision_query(q) and any(signal in q for signal in ["哪些", "历史上", "所有", "列表"])

    @staticmethod
    def _normalize_hybrid_hit(hit: dict) -> dict:
        """Convert a hybrid-retriever doc to the legacy format expected downstream.

        Hybrid docs have ``metadata.source`` / ``metadata.record_id``;
        legacy docs have top-level ``source`` / ``record``.  This method
        adds the top-level keys so the rest of the skill works unchanged.
        """
        meta = hit.get("metadata", {})
        if "source" not in hit:
            hit["source"] = meta.get("source", hit.get("id", "unknown"))
        if "record" not in hit:
            hit["record"] = meta.get("record_id", hit.get("id", "0"))
        # Ensure score exists
        if "score" not in hit:
            hit["score"] = float(hit.get("rrf_score", 0) * 100)
        return hit

    def _rank_docs(self, question, docs, decision_boost=0):
        # --- Primary: hybrid retrieval (FAISS vector + BM25 + entity → RRF) ---
        from src.skills.utils import hybrid_rank

        try:
            hybrid_hits = hybrid_rank(question, docs, top_k=20)
            for hit in hybrid_hits:
                self._normalize_hybrid_hit(hit)
                # Boost decision memory matches
                meta = hit.get("metadata", {})
                source = meta.get("source", hit.get("source", ""))
                if "decision_memory" in source:
                    hit["score"] = hit.get("score", 0) + decision_boost
            if hybrid_hits:
                return hybrid_hits
        except Exception:
            pass

        # --- Fallback: keyword + BM25 + entity (original pipeline) ---
        hits = []
        for doc in docs:
            score = keyword_score(question, doc["content"]) + entity_score(question, doc["content"], doc["source"])
            if score > 0:
                hit = dict(doc)
                hit["score"] = float(score + decision_boost)
                hits.append(hit)

        for hit in bm25_rank(question, docs):
            hit["score"] = hit.get("score", 0) + entity_score(question, hit["content"], hit["source"])
            if hit.get("memory_type") == "decision":
                hit["score"] = hit.get("score", 0) + decision_boost
            hits.append(hit)

        unique = {}
        for hit in hits:
            key = (hit["source"], hit["record"])
            if key not in unique or hit["score"] > unique[key]["score"]:
                unique[key] = hit
        return sorted(unique.values(), key=lambda item: item["score"], reverse=True)

    def run(self, question, top_k=5):
        decision_docs = self._load_decision_docs()
        regular_docs = self._load_regular_docs()

        if self._is_broad_decision_query(question):
            ranked = []
            for index, doc in enumerate(decision_docs):
                hit = dict(doc)
                hit["score"] = float(40 - index)
                ranked.append(hit)
        elif self._is_decision_query(question):
            ranked = self._rank_docs(question, decision_docs, decision_boost=20)
            if len(ranked) < top_k:
                ranked.extend(self._rank_docs(question, regular_docs, decision_boost=0))
        else:
            ranked = self._rank_docs(question, regular_docs + decision_docs, decision_boost=8)

        ranked = ranked[:top_k]
        if not ranked:
            return format_skill_result("未在历史讨论或长期记忆中检索到相关记录。", [], [], extract_entities(question))

        evidence = [
            {
                "source": h["source"],
                "content": h["content"],
                "score": h["score"],
                "metadata": h.get("metadata", {}),
            }
            for h in ranked
        ]
        sources = []
        for hit in ranked:
            sources.append(hit["source"])
            if hit["source"] == "decision_memory":
                sources.extend(hit.get("metadata", {}).get("source_files", []))
        entities = extract_entities(question + " " + " ".join(h["content"] for h in ranked))
        return format_skill_result("已从长期记忆中整理出相关历史讨论和技术决策。", evidence, sources, entities)
