"""Prepare and verify real FAISS indexes for the frozen Phase 4 snapshot.

The benchmark materializes a read-only Evidence database, then builds one
versioned live index per project scope.  Indexes are built from the frozen
Evidence only; no task label, Gold fact or expected answer enters this step.

The embedding model is loaded from the local Hugging Face cache with offline
mode forced, so index preparation cannot silently download a model.  Tests may
inject a deterministic embedder into :class:`IndexBuilder`, but FAISS itself is
always real: the production path writes, loads and verifies the index files.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.evidence import EvidenceScope
from src.index.builder import IndexBuilder

from .dataset import Snapshot

LOCAL_MODEL_ENV = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")


class IndexPreparationError(RuntimeError):
    """The requested retrieval mode cannot be executed over a verified index."""


@contextmanager
def _local_model_only():
    previous = {key: os.environ.get(key) for key in LOCAL_MODEL_ENV}
    try:
        for key in LOCAL_MODEL_ENV:
            os.environ[key] = "1"
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def project_ids(snapshot: Snapshot) -> list[str]:
    return sorted({record.project_id for record in snapshot.records})


def prepare_indexes(evidence_db: Path | str, snapshot: Snapshot, *,
                    index_root: Path | str | None = None,
                    embedder: Any = None) -> dict[str, Any]:
    """Build and verify one live index per project.

    Raises :class:`IndexPreparationError` on any build or verification failure.
    It never falls back to BM25: a failed preparation must abort the formal
    dense/hybrid comparison instead of producing a mislabelled result.
    """
    try:
        with _local_model_only():
            builder = IndexBuilder(evidence_db, index_root=index_root, embedder=embedder)
            entries = []
            for project_id in project_ids(snapshot):
                scope = EvidenceScope(project_id=project_id, dataset_kind="live")
                try:
                    built = builder.build(scope)
                except Exception as exc:  # bounded diagnostic, no raw payload
                    raise IndexPreparationError(
                        f"index build failed for {project_id}: "
                        f"{type(exc).__name__}: {exc}") from exc
                verified = builder.verify(scope)
                if not verified.get("ok"):
                    raise IndexPreparationError(
                        f"index verification failed for {project_id}: "
                        f"{', '.join(verified.get('problems') or ['unknown'])}")
                version = verified["version"]
                manifest = verified.get("manifest") or {}
                entries.append({
                    "project_id": project_id,
                    "dataset_kind": "live",
                    "build_status": built.get("status"),
                    "index_version": version["index_version"],
                    "source_revision": version["source_revision"],
                    "embedding_model": version["embedding_model"],
                    "embedding_dim": version["embedding_dim"],
                    "chunker_version": version["chunker_version"],
                    "evidence_count": version["evidence_count"],
                    "chunk_count": version["chunk_count"],
                    "faiss_sha256": manifest.get("files", {}).get("faiss.index"),
                    "chunks_sha256": manifest.get("files", {}).get("chunks.jsonl"),
                    "verified": True,
                })
    except IndexPreparationError:
        raise
    except Exception as exc:
        raise IndexPreparationError(
            f"index preparation failed: {type(exc).__name__}: {exc}") from exc
    return {
        "status": "ready",
        "index_count": len(entries),
        "embedding_model": entries[0]["embedding_model"] if entries else None,
        "embedding_dim": entries[0]["embedding_dim"] if entries else None,
        "chunk_count": sum(entry["chunk_count"] for entry in entries),
        "indexes": entries,
        "verification": "manifest_and_source_revision_checked",
    }


def summary(index_report: dict[str, Any] | None) -> dict[str, Any]:
    """Return a compact, report-safe view (no temporary filesystem paths)."""
    if not index_report:
        return {"status": "not_required"}
    return {
        "status": index_report.get("status"),
        "index_count": index_report.get("index_count", 0),
        "embedding_model": index_report.get("embedding_model"),
        "embedding_dim": index_report.get("embedding_dim"),
        "chunk_count": index_report.get("chunk_count", 0),
        "indexes": [
            {key: entry.get(key) for key in (
                "project_id", "index_version", "source_revision", "build_status",
                "embedding_model", "embedding_dim", "chunk_count", "verified")}
            for entry in index_report.get("indexes", [])
        ],
    }
