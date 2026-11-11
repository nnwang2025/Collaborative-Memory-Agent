"""
Hybrid retrieval engine for the Collaborative Memory Agent.

Combines three retrieval signals with Reciprocal Rank Fusion (RRF):

  1. **Dense (vector)** – bge-small-zh-v1.5 embeddings via FAISS.
     Captures semantic similarity for fuzzy natural-language queries.

  2. **Sparse (BM25)** – Classic lexical retrieval.
     Captures exact keyword matches, file names, issue IDs.

  3. **Entity** – Pre-indexed entity memory.
     Boosts documents that mention entities referenced in the query.

The three result lists are fused with RRF and optionally re-ranked with
a cross-encoder (BGE-Reranker).

Usage
-----
    retriever = HybridRetriever()
    retriever.index_all()        # one-time indexing
    results = retriever.retrieve("为什么采用混合检索", top_k=10)
"""

from __future__ import annotations

import json
import math
import os
import pickle
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]  # src/retrieval.py → Collaborative memory agent/
INDEX_DIR = ROOT / "db_md"
INDEX_PATH = INDEX_DIR / "faiss.index"
DOCS_PATH = INDEX_DIR / "docs.pkl"


class RetrievalHits(list):
    """A result list that also carries request-local retrieval diagnostics.

    It remains a normal list for existing callers, but lets the agent record
    why a request returned no evidence (empty candidates, relevance filtering,
    stale-index fallback) without adding fake Evidence records.
    """

    def __init__(self, items=(), diagnostics=None):
        super().__init__(items)
        self.diagnostics = dict(diagnostics or {})

# ---------------------------------------------------------------------------
# Document loading helpers
# ---------------------------------------------------------------------------

def _iter_data_sources() -> List[Tuple[Path, str]]:
    """Yield (path, category) for every JSON / Markdown data source."""
    sources: List[Tuple[Path, str]] = []

    # Decision memory
    dm = ROOT / "data" / "decision_memory.json"
    if dm.exists():
        sources.append((dm, "decision_memory"))

    # Semantic / episodic / entity / relationship memory
    for name in ["memory_records", "episodic_summaries", "entity_memory",
                  "relationship_memory"]:
        p = ROOT / "data" / f"{name}.json"
        if p.exists():
            sources.append((p, name))

    # Chats
    chats_dir = ROOT / "data" / "chats"
    if chats_dir.is_dir():
        for f in sorted(chats_dir.glob("*.json")):
            sources.append((f, "chats"))

    # Issues
    issues_dir = ROOT / "data" / "issues"
    if issues_dir.is_dir():
        for f in sorted(issues_dir.glob("*.json")):
            sources.append((f, "issues"))

    # Commits
    commits_dir = ROOT / "data" / "commits"
    if commits_dir.is_dir():
        for f in sorted(commits_dir.glob("*.json")):
            sources.append((f, "commits"))

    # Logs
    logs_dir = ROOT / "data" / "logs"
    if logs_dir.is_dir():
        for f in sorted(logs_dir.glob("*.json")):
            sources.append((f, "logs"))

    # Docs (Markdown)
    docs_dir = ROOT / "docs"
    if docs_dir.is_dir():
        for f in sorted(docs_dir.glob("*.md")):
            sources.append((f, "docs"))

    return sources


def _read_json_records(path: Path) -> List[dict]:
    """Load JSON records, handling list-of-dict and dict-of-list formats."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("records", "items", "data", "issues", "commits", "logs",
                     "messages"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def _flatten(obj) -> str:
    """Recursively flatten a JSON value into a searchable string."""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    if isinstance(obj, list):
        return " ".join(_flatten(v) for v in obj)
    if isinstance(obj, dict):
        return " ".join(_flatten(v) for v in obj.values())
    return str(obj)


# ---------------------------------------------------------------------------
# Tokenization helpers (for BM25)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Tokenize: CJK chars individually, ASCII words kept together."""
    import re
    text = (text or "").lower()
    return re.findall(r"[a-zA-Z0-9_./#-]+|[一-鿿]", text)


# ---------------------------------------------------------------------------
# Embedding model wrapper
# ---------------------------------------------------------------------------

_MODEL_NAME = "BAAI/bge-small-zh-v1.5"
_embedder: object | None = None
_embedder_lock: object = None  # threading.Lock (lazy-imported)


def _get_embedder():
    """Lazy-load the embedding model (singleton, thread-safe)."""
    global _embedder, _embedder_lock
    if _embedder is not None:
        return _embedder

    # Thread-safe init with double-checked locking
    import threading
    if _embedder_lock is None:
        _embedder_lock = threading.Lock()

    with _embedder_lock:
        if _embedder is None:
            from sentence_transformers import SentenceTransformer
            _embedder = SentenceTransformer(_MODEL_NAME)
    return _embedder


def get_embedding_dim() -> int:
    """Return embedding dimension (512 for bge-small-zh-v1.5)."""
    return 512


# ---------------------------------------------------------------------------
# Document builder
# ---------------------------------------------------------------------------

def _build_documents() -> List[dict]:
    """Convert all project data sources into a flat document list.

    Uses category-aware chunking:
      - Markdown → recursive split by headers/paragraphs/sentences (512 chars, 64 overlap)
      - Chats     → split by speaker turns (384 chars, 48 overlap)
      - Logs      → time-window grouping (512 chars max)
      - JSON records → content-aware split for long fields
      - Default   → recursive split

    Each document has:
      - id: unique string
      - content: chunked text for embedding / BM25
      - metadata: source, category, chunk info
    """
    from src.chunking import chunk_for_category

    docs: List[dict] = []
    seen: set = set()
    _chunk_id_counter = 0

    for path, category in _iter_data_sources():
        source = str(path.relative_to(ROOT))
        base_meta = {"source": source, "category": category}

        # --- Category 1: Markdown / docs → structural recursive split ---
        if category in ("docs",):
            text = path.read_text(encoding="utf-8")
            chunks = chunk_for_category("markdown", text, metadata=base_meta)
            for ci, ch in enumerate(chunks):
                doc_id = f"{path.stem}__c{ci}"
                if doc_id in seen:
                    # Avoid exact duplicate doc_ids (can happen with small files)
                    doc_id = f"{path.stem}__c{ci}_{_chunk_id_counter}"
                    _chunk_id_counter += 1
                seen.add(doc_id)
                docs.append({
                    "id": doc_id,
                    "content": ch["content"],
                    "metadata": {**base_meta, **ch.get("metadata", {}),
                                 "chunk_index": ci},
                })
            continue

        # --- Category 2: Chats → speaker turn split ---
        if category in ("chats",):
            records = _read_json_records(path)
            for ri, record in enumerate(records):
                content = record.get("content") or record.get("message") or _flatten(record)
                chunks = chunk_for_category("conversation", content, metadata=base_meta)
                for ci, ch in enumerate(chunks):
                    doc_id = f"{category}__r{ri}_c{ci}"
                    if doc_id in seen:
                        doc_id = f"{category}__r{ri}_c{ci}_{_chunk_id_counter}"
                        _chunk_id_counter += 1
                    seen.add(doc_id)
                    docs.append({
                        "id": doc_id,
                        "content": ch["content"],
                        "metadata": {**base_meta, "record_id": str(ri),
                                     "chunk_index": ci},
                    })
            continue

        # --- Category 3: Logs → time-window grouping ---
        if category in ("logs",):
            text = path.read_text(encoding="utf-8")
            chunks = chunk_for_category("logs", text, metadata=base_meta)
            for ci, ch in enumerate(chunks):
                doc_id = f"{path.stem}__c{ci}"
                if doc_id in seen:
                    doc_id = f"{path.stem}__c{ci}_{_chunk_id_counter}"
                    _chunk_id_counter += 1
                seen.add(doc_id)
                docs.append({
                    "id": doc_id,
                    "content": ch["content"],
                    "metadata": {**base_meta, "chunk_index": ci},
                })
            continue

        # --- Category 4: JSON records (decisions, issues, commits, memory, etc.) ---
        records = _read_json_records(path)
        for ri, record in enumerate(records):
            record_id = record.get("id") or record.get("record_id") or ri

            # Build rich content from priority fields
            parts = []
            for key in ("summary", "content", "message", "decision", "title",
                        "description", "discussion", "fix", "resolution"):
                val = record.get(key)
                if val:
                    parts.append(_flatten(val))
            content = " | ".join(parts) if parts else _flatten(record)

            # If content is short enough, keep as one document
            if len(content) <= 512:
                doc_id = f"{category}__{record_id}"
                if doc_id in seen:
                    doc_id = f"{category}__{record_id}_{_chunk_id_counter}"
                    _chunk_id_counter += 1
                seen.add(doc_id)
                docs.append({
                    "id": doc_id,
                    "content": content,
                    "metadata": {
                        "source": source,
                        "category": category,
                        "record_id": str(record_id),
                        "raw_json": json.dumps(record, ensure_ascii=False),
                    },
                })
            else:
                # Long JSON record → chunk it while preserving context
                chunks = chunk_for_category("json_record", content, metadata=base_meta)
                for ci, ch in enumerate(chunks):
                    doc_id = f"{category}__{record_id}__c{ci}"
                    if doc_id in seen:
                        doc_id = f"{category}__{record_id}__c{ci}_{_chunk_id_counter}"
                        _chunk_id_counter += 1
                    seen.add(doc_id)
                    docs.append({
                        "id": doc_id,
                        "content": ch["content"],
                        "metadata": {
                            "source": source,
                            "category": category,
                            "record_id": str(record_id),
                            "chunk_index": ci,
                            "raw_json": json.dumps(record, ensure_ascii=False),
                        },
                    })

    return docs


# ---------------------------------------------------------------------------
# FAISS Indexer
# ---------------------------------------------------------------------------

class FAISSIndexer:
    """Builds and persists a FAISS index over project documents."""

    def __init__(self, index_path: Path = INDEX_PATH, docs_path: Path = DOCS_PATH):
        self.index_path = index_path
        self.docs_path = docs_path

    def _ensure_dir(self):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

    def build(self, reset: bool = False) -> Tuple[object, List[dict]]:
        """Build (or load cached) FAISS index and document list.

        Returns
        -------
        (faiss_index, documents)
        """
        if not reset and self.index_path.exists() and self.docs_path.exists():
            return self.load()

        docs = _build_documents()
        if not docs:
            raise RuntimeError("No documents found to index.")

        # IMPORTANT: load sentence_transformers model BEFORE importing faiss.
        # On some platforms (especially Windows) the OpenMP runtimes of
        # faiss and sentence_transformers conflict unless loaded in this order.
        model = _get_embedder()

        import faiss  # noqa: E402  (lazy import – must be after _get_embedder)

        contents = [d["content"] for d in docs]

        # Batch-encode (no progress bar – avoids tqdm segfault on win32)
        print(f"  Embedding {len(contents)} documents with {_MODEL_NAME}...")
        all_embs = []
        for start in range(0, len(contents), 64):
            batch = contents[start : start + 64]
            e = model.encode(batch, normalize_embeddings=True).astype(np.float32)
            all_embs.append(e)
        embeddings = np.vstack(all_embs)

        dim = embeddings.shape[1]
        index = faiss.IndexFlatIP(dim)  # Inner Product (cosine with normalized vecs)
        index.add(embeddings)

        self._ensure_dir()
        faiss.write_index(index, str(self.index_path))
        with open(self.docs_path, "wb") as f:
            pickle.dump(docs, f)

        print(f"  Indexed {index.ntotal} vectors (dim={dim}) → {self.index_path}")
        return index, docs

    def load(self) -> Tuple[object, List[dict]]:
        """Load cached index and documents from disk.

        IMPORTANT: the caller must ensure ``_get_embedder()`` has been
        called *before* this method (so sentence_transformers initialises
        before faiss touches the OpenMP runtime).
        """
        # Ensure model is loaded first (idempotent)
        _get_embedder()

        import faiss  # noqa: E402

        index = faiss.read_index(str(self.index_path))
        with open(self.docs_path, "rb") as f:
            docs = pickle.load(f)
        return index, docs

    def load_or_build(self) -> Tuple[object, List[dict]]:
        """Load cached; build if missing."""
        return self.build(reset=False)


# ---------------------------------------------------------------------------
# Sparse / entity retrieval
# ---------------------------------------------------------------------------

def _bm25_rank(
    query: str,
    docs: List[dict],
    top_k: int = 20,
) -> List[dict]:
    """From-scratch BM25 (k1=1.5, b=0.75)."""
    query_tokens = _tokenize(query)
    if not query_tokens or not docs:
        return []

    # retrieval_text includes the stable record identifier but is used only for
    # ranking; ``content`` stays the citable parent text.
    tokenized_docs = [_tokenize(d.get("retrieval_text") or d["content"]) for d in docs]
    doc_count = len(tokenized_docs)
    avgdl = sum(len(t) for t in tokenized_docs) / max(doc_count, 1)
    df = Counter()
    for tokens in tokenized_docs:
        df.update(set(tokens))

    k1, b = 1.5, 0.75
    ranked = []
    for doc, tokens in zip(docs, tokenized_docs):
        tf = Counter(tokens)
        score = 0.0
        doc_len = len(tokens) or 1
        for token in query_tokens:
            if token not in tf:
                continue
            idf = math.log(1 + (doc_count - df[token] + 0.5) / (df[token] + 0.5))
            denom = tf[token] + k1 * (1 - b + b * doc_len / max(avgdl, 1))
            score += idf * (tf[token] * (k1 + 1) / denom)
        if score > 0:
            item = dict(doc)
            item["bm25_score"] = round(score, 4)
            ranked.append(item)

    ranked.sort(key=lambda d: d["bm25_score"], reverse=True)
    return ranked[:top_k]


def _load_entities() -> List[str]:
    """Load known entity names from entity_memory.json."""
    path = ROOT / "data" / "entity_memory.json"
    records = _read_json_records(path)
    entities = []
    for r in records:
        name = r.get("entity", "")
        if name:
            entities.append(str(name))
        for alias in r.get("aliases", []):
            entities.append(str(alias))
    return sorted(set(entities), key=len, reverse=True)


def _entity_boost(query: str, docs: List[dict]) -> List[dict]:
    """Score docs by how many query-mentioned entities they contain."""
    entities = _load_entities()
    query_lower = (query or "").lower()
    hits = [e for e in entities if e.lower() in query_lower]
    if not hits:
        return []

    boosted = []
    for doc in docs:
        text = (doc.get("content", "")).lower()
        score = 0.0
        for entity in hits:
            if entity.lower() in text:
                score += 30.0
        meta_source = (
            doc.get("metadata", {}).get("source", "")
            or doc.get("source", "")
        ).lower()
        if score and any(k in meta_source for k in (
            "decision_memory", "docs/", "issues/", "commits/"
        )):
            score += 20.0
        if score > 0:
            item = dict(doc)
            item["entity_score"] = score
            boosted.append(item)

    boosted.sort(key=lambda d: d["entity_score"], reverse=True)
    return boosted


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(
    result_lists: List[List[dict]],
    k: int = 60,
    id_key: str = "id",
) -> List[dict]:
    """Fuse multiple ranked result lists with RRF.

    score(doc) = Σ 1/(k + rank_in_list_i)
    """
    rrf_scores: Dict[str, float] = {}
    doc_map: Dict[str, dict] = {}

    for ranked in result_lists:
        for rank, doc in enumerate(ranked, start=1):
            doc_id = doc.get(id_key, str(rank))
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            if doc_id not in doc_map:
                doc_map[doc_id] = dict(doc)

    fused = []
    for doc_id, rrf in rrf_scores.items():
        item = dict(doc_map[doc_id])
        item["rrf_score"] = round(rrf, 6)
        for prefix in ("bm25_score", "entity_score", "vector_score"):
            item.setdefault(prefix, 0.0)
        fused.append(item)

    fused.sort(key=lambda d: d["rrf_score"], reverse=True)
    return fused


# ---------------------------------------------------------------------------
# Hybrid Retriever (public API)
# ---------------------------------------------------------------------------

class HybridRetriever:
    """Hybrid retrieval: dense (FAISS+bge-small) + sparse (BM25) + entity → RRF.

    Parameters
    ----------
    index_path:
        Path to the FAISS index file.
    docs_path:
        Path to the pickled document list.
    """

    def __init__(
        self,
        index_path: Path | str = INDEX_PATH,
        docs_path: Path | str = DOCS_PATH,
    ):
        self.index_path = Path(index_path)
        self.docs_path = Path(docs_path)
        self._index: object | None = None
        self._docs: List[dict] = []
        self._loaded = False

    # -- Lazy init ----------------------------------------------------------

    def _ensure_loaded(self):
        """Load FAISS index + docs (once)."""
        if self._loaded:
            return
        indexer = FAISSIndexer(self.index_path, self.docs_path)
        if self.index_path.exists() and self.docs_path.exists():
            self._index, self._docs = indexer.load()
        else:
            self._index, self._docs = indexer.build()
        self._loaded = True

    # -- Indexing -----------------------------------------------------------

    def index_all(self, reset: bool = False) -> int:
        """(Re)build the FAISS index and return document count."""
        if reset:
            if self.index_path.exists():
                self.index_path.unlink()
            if self.docs_path.exists():
                self.docs_path.unlink()
            self._loaded = False

        indexer = FAISSIndexer(self.index_path, self.docs_path)
        self._index, self._docs = indexer.build(reset=reset)
        self._loaded = True
        return len(self._docs)

    @property
    def docs(self) -> List[dict]:
        """Return the full document list (loaded on first access)."""
        self._ensure_loaded()
        return self._docs

    # -- Retrieval ----------------------------------------------------------

    def dense_search(self, query: str, top_k: int = 20) -> List[dict]:
        """Vector similarity search via FAISS."""
        self._ensure_loaded()
        if not self._docs or self._index is None:
            return []

        model = _get_embedder()
        query_vec = model.encode(
            [query], normalize_embeddings=True
        ).astype(np.float32)

        scores, indices = self._index.search(query_vec, min(top_k, len(self._docs)))  # type: ignore[union-attr]

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or idx >= len(self._docs):
                continue
            doc = dict(self._docs[idx])
            doc["vector_score"] = round(float(score), 4)
            results.append(doc)

        return results

    def sparse_search(self, query: str, top_k: int = 20) -> List[dict]:
        """BM25 lexical search."""
        self._ensure_loaded()
        return _bm25_rank(query, self._docs, top_k=top_k)

    def entity_search(self, query: str, top_k: int = 20) -> List[dict]:
        """Entity-boosted search."""
        self._ensure_loaded()
        return _entity_boost(query, self._docs)[:top_k]

    def retrieve(
        self,
        query: str,
        top_k: int = 10,
        dense_on: bool = True,
        sparse_on: bool = True,
        entity_on: bool = True,
        *, scope=None, db_path=None, live_dir=None,
    ) -> List[dict]:
        """Hybrid retrieval with RRF fusion.

        Parameters
        ----------
        query:
            Natural-language search query.
        top_k:
            Number of results after fusion.
        dense_on / sparse_on / entity_on:
            Toggle individual retrieval channels (useful for ablation).

        Returns
        -------
        list[dict]
            Ranked documents with ``rrf_score`` and per-channel scores.
        """
        if scope is not None:
            return retrieve_evidence(query, scope=scope, db_path=db_path,
                                     live_dir=live_dir, top_k=top_k)
        result_lists: List[List[dict]] = []

        if dense_on:
            result_lists.append(self.dense_search(query, top_k=top_k * 2))
        if sparse_on:
            result_lists.append(self.sparse_search(query, top_k=top_k * 2))
        if entity_on:
            result_lists.append(self.entity_search(query, top_k=top_k * 2))

        if not result_lists:
            return []

        fused = reciprocal_rank_fusion(result_lists)
        return fused[:top_k]


# ---------------------------------------------------------------------------
# Convenience: drop-in replacement for bm25_rank() in skills
# ---------------------------------------------------------------------------

_shared: HybridRetriever | None = None


def get_hybrid_retriever() -> HybridRetriever:
    """Module-level singleton."""
    global _shared
    if _shared is None:
        _shared = HybridRetriever()
    return _shared


def hybrid_rank(
    query: str,
    docs: Optional[List[dict]] = None,
    top_k: int = 10,
    *, scope=None, db_path=None, live_dir=None,
) -> List[dict]:
    """Hybrid rank – drop-in replacement for ``bm25_rank()``.

    If *docs* is provided, runs BM25 + entity on that subset, then fuses
    with dense results from the full FAISS index via RRF.

    If *docs* is None, retrieves from the full index.
    """
    if scope is not None:
        return retrieve_evidence(query, scope=scope, db_path=db_path, live_dir=live_dir, top_k=top_k)
    retriever = get_hybrid_retriever()

    result_lists: List[List[dict]] = []

    # Subset calls must never introduce evidence outside the requested corpus.
    # Canonical IDs also prevent unrelated legacy docs merging by rank position.
    if docs is not None and not docs:
        return []
    target_docs = []
    for original in docs if docs is not None else retriever.docs:
        item = dict(original)
        if "id" not in item:
            item["id"] = json.dumps([item.get("source"), item.get("record"),
                                     item.get("paragraph"), item.get("content")], ensure_ascii=False)
        target_docs.append(item)
    dense_hits = retriever.dense_search(query, top_k=top_k * 2)
    if docs is not None:
        # Preserve original record metadata and content; match only exact text.
        by_content = {}
        for item in target_docs:
            by_content.setdefault(item["content"], []).append(item)
        dense_hits = [dict(item) for hit in dense_hits
                      for item in by_content.get(hit["content"], [])]
    result_lists.append(dense_hits)
    result_lists.append(_bm25_rank(query, target_docs, top_k=top_k * 2))
    result_lists.append(_entity_boost(query, target_docs)[:top_k * 2])

    fused = reciprocal_rank_fusion(result_lists)
    return fused[:top_k]


def retrieve_evidence(query, *, scope, db_path=None, live_dir=None, source_types=None,
                      top_k=10, filters=None, mode=None, index_root=None,
                      relevance_policy=None):
    """Explicitly scoped live retrieval with final relevance acceptance.

    The default remains BM25 for V1 compatibility.  ``mode`` may be ``bm25``,
    ``dense`` or ``hybrid``; the environment variable ``LIVE_RETRIEVAL_MODE``
    is used when it is omitted.  Hybrid/dense failures return an explicitly
    labelled BM25 fallback rather than pretending fusion succeeded.

    Candidate recall and final acceptance are separate.  Every returned parent
    Evidence must pass the calibrated :mod:`src.index.relevance` gate; when
    nothing passes, the returned :class:`RetrievalHits` list is empty and its
    ``diagnostics`` explain whether there were no candidates or all candidates
    were filtered out.
    """
    from src.evidence import EvidenceScope
    scope = EvidenceScope.model_validate(scope)
    if scope.dataset_kind != "live":
        return RetrievalHits()  # fixture demo remains on the existing Skill path
    resolved_mode = (mode or os.getenv("LIVE_RETRIEVAL_MODE") or "bm25").lower()
    from src.index.retriever import LiveRetriever
    retriever = LiveRetriever(db_path, index_root=index_root, live_dir=live_dir,
                              mode=resolved_mode,
                              relevance_policy=relevance_policy)
    return retriever.retrieve(
        query, scope=scope, top_k=top_k, mode=resolved_mode,
        source_types=source_types, filters=filters,
    )


def filter_evidence_hits(hits, filters):
    from datetime import datetime
    from src.investigation import Filters
    filters = filters if isinstance(filters, Filters) else Filters.model_validate(filters)
    after = datetime.fromisoformat(filters.after.replace("Z", "+00:00")) if filters.after else None
    before = datetime.fromisoformat(filters.before.replace("Z", "+00:00")) if filters.before else None
    result = []
    for hit in hits:
        if filters.source_types and hit.get("source_type") not in filters.source_types:
            continue
        if filters.evidence_ids and hit.get("evidence_id") not in filters.evidence_ids:
            continue
        event_time = hit.get("event_time")
        if filters.undated_only and event_time is not None:
            continue
        date = datetime.fromisoformat(event_time.replace("Z", "+00:00")) if isinstance(event_time, str) else event_time
        if (after or before) and date is None:
            continue
        if after and date < after or before and date > before:
            continue
        result.append(hit)
    return result


def get_evidence(evidence_id, *, scope, db_path=None, live_dir=None, content_hash=None):
    """Re-fetch full evidence within the exact partition, optionally by version hash."""
    from src.evidence import EvidenceScope
    from src.evidence_store import EvidenceStore, read_live_json
    scope = EvidenceScope.model_validate(scope)
    store = EvidenceStore(db_path)
    found = store.get(evidence_id, scope, content_hash)
    if found is not None:
        return found
    if scope.dataset_kind == "live":
        for record in read_live_json(live_dir or store.path.parent, scope):
            if record.evidence_id == evidence_id and (content_hash is None or record.content_hash == content_hash):
                return record
    return None
