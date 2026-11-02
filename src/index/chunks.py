"""Deterministic chunk construction from full Evidence records.

Chunks are derived retrieval units.  They always carry the parent Evidence ID
and content hash, and can never be cited without re-fetching and validating the
parent.
"""

from __future__ import annotations

import hashlib
import json

from src.chunking import CHUNKER_VERSION, chunk_with_offsets
from src.evidence import Evidence
from src.evidence_store import utcnow
from src.retrieval import _tokenize

_CATEGORY_ROUTES = {
    "markdown": "markdown", "docs": "markdown", "document": "markdown",
    "chats": "conversation", "chat": "conversation", "conversation": "conversation",
    "logs": "logs", "log": "logs",
    "code": "code",
    "issue": "json_record", "issues": "json_record", "commit": "json_record",
    "commits": "json_record", "json_record": "json_record",
}


def chunker_category(evidence: Evidence) -> str:
    metadata = evidence.metadata or {}
    for value in (metadata.get("category"), evidence.source_type):
        if value and str(value).lower() in _CATEGORY_ROUTES:
            return _CATEGORY_ROUTES[str(value).lower()]
    return "default"


def stable_chunk_id(evidence: Evidence, chunk_index: int, chunker_version: str) -> str:
    identity = json.dumps(
        [evidence.project_id, evidence.dataset_kind, evidence.evidence_id,
         evidence.content_hash, chunker_version, chunk_index],
        ensure_ascii=False, separators=(",", ":"),
    )
    return "chk_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def build_chunks(evidence: Evidence, *, chunker_version: str = CHUNKER_VERSION) -> list[dict]:
    """Return deterministic chunk records without writing them."""
    if not evidence.content or not evidence.content.strip():
        return []
    pieces = chunk_with_offsets(
        chunker_category(evidence), evidence.content,
        metadata={"evidence_id": evidence.evidence_id,
                  "content_hash": evidence.content_hash},
        chunker_version=chunker_version,
    )
    chunks = []
    for index, piece in enumerate(pieces):
        text = piece["content"]
        metadata = piece["metadata"]
        chunk_id = stable_chunk_id(evidence, index, chunker_version)
        # Retrieval may use the stable record identifier, but citable chunk text
        # stays a verbatim slice of the parent Evidence.  This prevents an
        # exact-ID query from matching only an unrelated commit message.
        original_record_id = evidence.original_record_id
        retrieval_text = (f"{original_record_id}\n{text}"
                          if original_record_id else text)
        chunk = {
            "chunk_id": chunk_id,
            "project_id": evidence.project_id,
            "dataset_kind": evidence.dataset_kind,
            "evidence_id": evidence.evidence_id,
            "content_hash": evidence.content_hash,
            "original_record_id": original_record_id,
            "chunk_index": index,
            "char_start": metadata.get("char_start"),
            "char_end": metadata.get("char_end"),
            "line_start": metadata.get("line_start"),
            "line_end": metadata.get("line_end"),
            "text": text,
            "retrieval_text": retrieval_text,
            "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "token_count": len(_tokenize(text)),
            "chunker_version": chunker_version,
            "locator_status": metadata.get("locator_status", "unavailable"),
            "created_at": utcnow(),
            "locator": {
                "char_start": metadata.get("char_start"),
                "char_end": metadata.get("char_end"),
                "line_start": metadata.get("line_start"),
                "line_end": metadata.get("line_end"),
                "status": metadata.get("locator_status", "unavailable"),
            },
        }
        chunks.append(chunk)
    return chunks
