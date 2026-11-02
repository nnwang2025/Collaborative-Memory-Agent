"""Live hybrid retrieval over active Evidence and versioned chunk indexes.

Dense/sparse hits are candidate retrieval units.  Every candidate is hydrated
back to full Evidence and re-checked against the current state and content hash
before it can be returned.  Candidate recall is followed by a separate,
calibrated relevance-acceptance gate; an index problem falls back to BM25 and
labels the failure explicitly; it is never reported as a successful hybrid run.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np

from src.evidence import Evidence, EvidenceScope
from src.evidence_store import EvidenceStore, default_db_path, read_live_json
from src.index.relevance import (
    identifier_match,
    lexical_coverage,
    load_relevance_policy,
)
from src.index.store import IndexStore
from src.retrieval import (
    RetrievalHits,
    _bm25_rank,
    _get_embedder,
    filter_evidence_hits,
    reciprocal_rank_fusion,
)

INDEX_ROOT_ENV = "LIVE_INDEX_ROOT"
RETRIEVAL_MODE_ENV = "LIVE_RETRIEVAL_MODE"
VALID_MODES = {"bm25", "dense", "hybrid"}


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parent_retrieval_text(parent) -> str:
    identifier = parent.original_record_id or ""
    return f"{identifier}\n{parent.content}" if identifier else parent.content


def _decision_record(decision, policy):
    return {
        **decision,
        "policy_version": policy.policy_version,
        "thresholds": {
            "dense_min_score": float(policy.dense_min_score),
            "lexical_min_coverage": float(policy.lexical_min_coverage),
        },
    }


class LiveRetriever:
    def __init__(self, db_path, *, index_root=None, embedder=None, mode=None,
                 live_dir=None, relevance_policy=None):
        self.db_path = (Path(db_path) if db_path is not None
                        else default_db_path())
        self.store = EvidenceStore(self.db_path)
        # Index access is lazy: plain BM25 retrieval must not create a database
        # or an index store as a side effect of a read-only query.
        self._index_store = None
        self.index_root = Path(index_root or os.getenv(INDEX_ROOT_ENV)
                               or self.db_path.parent / "live_index")
        self.embedder = embedder
        self.live_dir = live_dir
        self.mode = (mode or os.getenv(RETRIEVAL_MODE_ENV) or "bm25").lower()
        self.relevance_policy = relevance_policy

    @property
    def index_store(self):
        if self._index_store is None:
            self._index_store = IndexStore(self.db_path)
        return self._index_store

    def _scope(self, scope):
        return EvidenceScope.model_validate(scope)

    def _policy(self):
        if self.relevance_policy is None:
            self.relevance_policy = load_relevance_policy()
        elif isinstance(self.relevance_policy, (str, Path)):
            self.relevance_policy = load_relevance_policy(self.relevance_policy)
        return self.relevance_policy

    def _embedder_instance(self):
        if self.embedder is None:
            self.embedder = _get_embedder()
        return self.embedder

    def _active_evidence(self, scope, source_types=None, filters=None):
        records = {r.evidence_id: r for r in read_live_json(
            self.live_dir or self.store.path.parent, scope)}
        for record in self.store.list(scope):
            records[record.evidence_id] = record
        active = {}
        for evidence_id, record in records.items():
            if source_types is not None and record.source_type not in source_types:
                continue
            state = self.store.current_state(evidence_id, scope)
            if state is not None and (state["state"] != "active"
                                      or state["content_hash"] != record.content_hash):
                continue
            active[evidence_id] = record
        docs = [record.as_hit() for record in active.values()]
        for doc in docs:
            doc["retrieval_text"] = _parent_retrieval_text(
                active[doc["evidence_id"]])
        if filters:
            docs = filter_evidence_hits(docs, filters)
            allowed_ids = {doc["evidence_id"] for doc in docs}
            active = {eid: record for eid, record in active.items() if eid in allowed_ids}
        return active, docs

    # -- public API ------------------------------------------------------
    def retrieve(self, query, *, scope, top_k=10, mode=None, source_types=None,
                 filters=None):
        hits, diagnostics = self.retrieve_with_diagnostics(
            query, scope=scope, top_k=top_k, mode=mode,
            source_types=source_types, filters=filters)
        hits.diagnostics = diagnostics
        return hits

    def retrieve_with_diagnostics(self, query, *, scope, top_k=10, mode=None,
                                  source_types=None, filters=None):
        scope = self._scope(scope)
        mode = (mode or self.mode).lower()
        if mode not in VALID_MODES:
            mode = "bm25"
        if scope.dataset_kind != "live":
            return RetrievalHits(), {
                "requested_mode": mode, "effective_mode": mode,
                "index_state": "not_applicable", "fallback_reason": "non_live_scope",
                "candidate_count": 0, "accepted_count": 0, "filtered_count": 0,
                "filter_reasons": {}, "empty_after_filtering": True,
                "empty_reason": "non_live_scope",
            }

        policy = self._policy()
        active, docs = self._active_evidence(scope, source_types, filters)
        if mode == "bm25":
            candidates = _bm25_rank(query, docs, top_k=top_k * 3)
            hits, diagnostics = self._accept_parent_hits(
                query, candidates, policy=policy, top_k=top_k,
                retrieval_mode="bm25", fallback_reason=None,
                index_state="not_required", index_version=None,
                source_revision=None)
            diagnostics.update({
                "requested_mode": mode, "effective_mode": "bm25",
                "index_state": "not_required", "fallback_reason": None,
            })
            return hits, diagnostics

        try:
            return self._index_retrieve(
                query, scope=scope, active=active, docs=docs, top_k=top_k,
                mode=mode, policy=policy,
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            candidates = _bm25_rank(query, docs, top_k=top_k * 3)
            hits, diagnostics = self._accept_parent_hits(
                query, candidates, policy=policy, top_k=top_k,
                retrieval_mode="bm25_fallback", fallback_reason=reason,
                index_state="unusable", index_version=None, source_revision=None)
            diagnostics.update({
                "requested_mode": mode, "effective_mode": "bm25_fallback",
                "index_state": "unusable", "fallback_reason": reason,
            })
            return hits, diagnostics

    # -- final acceptance -------------------------------------------------
    @staticmethod
    def _accept_parent_hits(query, candidates, *, policy, top_k, retrieval_mode,
                            fallback_reason, index_state, index_version,
                            source_revision):
        """Apply the final gate to already-hydrated parent Evidence hits."""
        accepted = []
        filtered = []
        reasons = {}
        for hit in candidates:
            text = hit.get("retrieval_text") or (
                f"{hit.get('original_record_id')}\n{hit.get('content', '')}")
            coverage = lexical_coverage(query, text)
            identifier = identifier_match(query, text)
            decision = policy.decision(
                dense_score=None, lexical_coverage=coverage,
                identifier_match=identifier)
            if decision["accepted"]:
                hit["score"] = hit.get("bm25_score", 0.0)
                hit["retrieval_mode"] = retrieval_mode
                hit["fallback_reason"] = fallback_reason
                hit["index_state"] = index_state
                hit["index_version"] = index_version
                hit["source_revision"] = source_revision
                hit["score_kind"] = "retrieval_relevance"
                hit["relevance"] = _decision_record(decision, policy)
                accepted.append(hit)
            else:
                reason = decision["filter_reason"] or "rejected"
                reasons[reason] = reasons.get(reason, 0) + 1
                filtered.append({
                    "original_record_id": hit.get("original_record_id"),
                    "evidence_id": hit.get("evidence_id"),
                    "dense_score": decision["dense_score"],
                    "lexical_coverage": decision["lexical_coverage"],
                    "identifier_match": decision["identifier_match"],
                    "reason": reason,
                })
        diagnostics = {
            "candidate_count": len(candidates),
            "accepted_count": len(accepted),
            "filtered_count": len(filtered),
            "filter_reasons": reasons,
            "rejected_examples": filtered[:3],
            "relevance_filtering_enabled": bool(policy.enabled),
            "policy_version": policy.policy_version,
            "policy_source": policy.source,
            "thresholds": {
                "dense_min_score": float(policy.dense_min_score),
                "lexical_min_coverage": float(policy.lexical_min_coverage),
            },
            "empty_after_filtering": not accepted,
            "empty_reason": (
                "no_candidates" if not candidates
                else "all_candidates_filtered" if not accepted else None),
            "index_version": index_version,
            "source_revision": source_revision,
        }
        return RetrievalHits(accepted[:top_k], diagnostics), diagnostics

    # -- index-backed recall ---------------------------------------------
    def _load_version(self, scope):
        version = self.index_store.active_version(scope)
        if version is None:
            raise FileNotFoundError("no_active_index")
        if version["source_revision"] != self.store.revision(scope):
            raise RuntimeError("stale_index_source_revision")
        base = Path(version["path"])
        manifest_path = base / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("index_manifest_missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("project_id") != scope.project_id
                or manifest.get("dataset_kind") != scope.dataset_kind):
            raise ValueError("index_scope_mismatch")
        for name, expected in manifest.get("files", {}).items():
            path = base / name
            if not path.is_file() or _sha256_file(path) != expected:
                raise ValueError(f"index_checksum_mismatch:{name}")
        chunks_path = base / "chunks.jsonl"
        chunks = [json.loads(line) for line in chunks_path.read_text(
            encoding="utf-8").splitlines() if line.strip()]
        if len(chunks) != manifest["chunk_count"]:
            raise ValueError("index_chunk_count_mismatch")
        for index, chunk in enumerate(chunks):
            if chunk["project_id"] != scope.project_id or chunk["dataset_kind"] != scope.dataset_kind:
                raise ValueError("index_chunk_scope_mismatch")
        import faiss
        index = faiss.read_index(str(base / "faiss.index"))
        if index.ntotal != len(chunks):
            raise ValueError("index_vector_count_mismatch")
        return version, manifest, chunks, index

    def _index_retrieve(self, query, *, scope, active, docs, top_k, mode, policy):
        version, manifest, chunks, index = self._load_version(scope)
        by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
        allowed_hashes = {eid: record.content_hash for eid, record in active.items()}
        eligible = [chunk for chunk in chunks
                    if chunk["evidence_id"] in allowed_hashes
                    and chunk["content_hash"] == allowed_hashes[chunk["evidence_id"]]]

        result_lists = []
        channel_ranks = {}
        if mode in {"hybrid", "dense"}:
            embedder = self._embedder_instance()
            model_name = str(getattr(embedder, "model_name", None)
                             or getattr(embedder, "_model_name", None)
                             or "BAAI/bge-small-zh-v1.5")
            if model_name != manifest.get("embedding_model"):
                raise ValueError(
                    f"embedding_model_mismatch:{model_name}!={manifest.get('embedding_model')}")
            query_vec = np.asarray(
                embedder.encode([query], normalize_embeddings=True),
                dtype=np.float32,
            )
            if query_vec.shape[1] != manifest["embedding_dim"]:
                raise ValueError("embedding_dimension_mismatch")
            scores, indices = index.search(query_vec, min(top_k * 3, len(chunks)))
            dense_hits = []
            for score, position in zip(scores[0], indices[0]):
                if position < 0 or position >= len(chunks):
                    continue
                chunk = chunks[int(position)]
                if chunk["evidence_id"] not in allowed_hashes:
                    continue
                if chunk["content_hash"] != allowed_hashes[chunk["evidence_id"]]:
                    continue
                dense_hits.append({
                    "id": chunk["chunk_id"],
                    "content": chunk.get("retrieval_text") or chunk["text"],
                    "chunk": chunk, "vector_score": float(score)})
            result_lists.append(dense_hits)
            channel_ranks["dense"] = {
                hit["id"]: {"rank": rank, "score": round(float(hit["vector_score"]), 6)}
                for rank, hit in enumerate(dense_hits, start=1)
            }
        if mode in {"hybrid", "bm25"}:
            # The retrieval-only text carries the stable record identifier; the
            # returned matched chunk text remains the citable parent slice.
            chunk_docs = [{
                "id": chunk["chunk_id"],
                "content": chunk.get("retrieval_text") or chunk["text"],
                "chunk": chunk,
            } for chunk in eligible]
            sparse_hits = _bm25_rank(query, chunk_docs, top_k=top_k * 3)
            result_lists.append(sparse_hits)
            channel_ranks["bm25"] = {
                hit["id"]: {"rank": rank, "score": round(float(hit["bm25_score"]), 6)}
                for rank, hit in enumerate(sparse_hits, start=1)
            }
        if not result_lists:
            raise ValueError("no_retrieval_channels_enabled")

        fused = reciprocal_rank_fusion(result_lists)
        by_parent = {}
        for candidate in fused:
            chunk = by_id.get(candidate["id"])
            if chunk is None:
                continue
            parent = active.get(chunk["evidence_id"])
            if parent is None or parent.content_hash != chunk["content_hash"]:
                continue
            channels = {
                name: ranks[chunk["chunk_id"]]
                for name, ranks in channel_ranks.items()
                if chunk["chunk_id"] in ranks
            }
            dense_rank = channel_ranks.get("dense", {}).get(chunk["chunk_id"])
            chunk_text = chunk.get("retrieval_text") or chunk["text"]
            chunk_coverage = lexical_coverage(query, chunk_text)
            chunk_identifier = identifier_match(query, chunk_text)
            entry = by_parent.get(parent.evidence_id)
            if entry is None:
                entry = {
                    "parent": parent, "rrf_score": candidate["rrf_score"],
                    "channels": {}, "matched_chunks": [],
                    "dense_score": None,
                }
                by_parent[parent.evidence_id] = entry
            entry["channels"].update(channels)
            if dense_rank is not None:
                score = dense_rank["score"]
                entry["dense_score"] = (score if entry["dense_score"] is None
                                        else max(entry["dense_score"], score))
            matched = {
                "chunk_id": chunk["chunk_id"], "chunk_index": chunk["chunk_index"],
                "text": chunk["text"], "char_start": chunk["char_start"],
                "char_end": chunk["char_end"], "line_start": chunk["line_start"],
                "line_end": chunk["line_end"],
                "locator_status": chunk["locator_status"],
                "channels": channels, "rrf_score": candidate["rrf_score"],
                "score": candidate["rrf_score"],
                "dense_score": None if dense_rank is None else dense_rank["score"],
                "lexical_coverage": chunk_coverage,
                "identifier_match": chunk_identifier,
                "_order": (
                    bool(chunk_identifier),
                    bool(chunk_coverage is not None
                         and chunk_coverage >= policy.lexical_min_coverage),
                    bool(dense_rank is not None
                         and dense_rank["score"] >= policy.dense_min_score),
                    chunk_coverage if chunk_coverage is not None else -1.0,
                    dense_rank["score"] if dense_rank is not None else -1.0,
                    candidate["rrf_score"],
                ),
            }
            entry["matched_chunks"].append(matched)

        accepted = []
        filtered = []
        reasons = {}
        for entry in sorted(by_parent.values(), key=lambda item: item["rrf_score"],
                            reverse=True):
            parent = entry["parent"]
            parent_text = _parent_retrieval_text(parent)
            coverage = lexical_coverage(query, parent_text)
            identifier = identifier_match(query, parent_text)
            decision = policy.decision(
                dense_score=entry["dense_score"], lexical_coverage=coverage,
                identifier_match=identifier)
            entry["matched_chunks"].sort(key=lambda item: item["_order"], reverse=True)
            for matched in entry["matched_chunks"]:
                matched.pop("_order", None)
            entry["matched_chunks"] = entry["matched_chunks"][:3]
            if not decision["accepted"]:
                reason = decision["filter_reason"] or "rejected"
                reasons[reason] = reasons.get(reason, 0) + 1
                filtered.append({
                    "original_record_id": parent.original_record_id,
                    "evidence_id": parent.evidence_id,
                    "dense_score": decision["dense_score"],
                    "lexical_coverage": decision["lexical_coverage"],
                    "identifier_match": decision["identifier_match"],
                    "reason": reason,
                })
                continue
            hit = parent.as_hit(score=entry["rrf_score"], source=parent.source_uri)
            hit.update({
                "retrieval_mode": mode,
                "fallback_reason": None,
                "index_state": "active",
                "index_version": version["index_version"],
                "source_revision": version["source_revision"],
                "rrf_score": entry["rrf_score"],
                "channels": entry["channels"],
                "matched_chunks": entry["matched_chunks"],
                "score_kind": "retrieval_relevance",
                "relevance": _decision_record(decision, policy),
            })
            accepted.append(hit)

        diagnostics = {
            "requested_mode": mode,
            "effective_mode": mode,
            "index_state": "active",
            "fallback_reason": None,
            "index_version": version["index_version"],
            "source_revision": version["source_revision"],
            "candidate_count": len(by_parent),
            "accepted_count": len(accepted),
            "filtered_count": len(filtered),
            "filter_reasons": reasons,
            "rejected_examples": filtered[:3],
            "relevance_filtering_enabled": bool(policy.enabled),
            "policy_version": policy.policy_version,
            "policy_source": policy.source,
            "thresholds": {
                "dense_min_score": float(policy.dense_min_score),
                "lexical_min_coverage": float(policy.lexical_min_coverage),
            },
            "empty_after_filtering": not accepted,
            "empty_reason": (
                "no_candidates" if not by_parent
                else "all_candidates_filtered" if not accepted else None),
        }
        return RetrievalHits(accepted[:top_k], diagnostics), diagnostics
