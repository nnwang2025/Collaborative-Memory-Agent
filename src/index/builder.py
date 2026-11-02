"""Build, verify, and atomically publish versioned live indexes.

The index is a disposable cache.  Evidence in SQLite remains authoritative and
every hit is re-checked against the current Evidence state before it is
returned to an agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path

import numpy as np

from src.chunking import CHUNKER_VERSION
from src.evidence import EvidenceScope
from src.evidence_store import EvidenceStore, utcnow
from src.index.chunks import build_chunks
from src.index.store import IndexStore


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_component(value):
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))[:64]
    suffix = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]
    return f"{cleaned or 'scope'}-{suffix}"


class NoIndexableEvidence(ValueError):
    pass


class IndexBuilder:
    def __init__(self, db_path, *, index_root=None, embedder=None,
                 chunker_version=CHUNKER_VERSION):
        self.db_path = Path(db_path)
        self.store = IndexStore(self.db_path)
        self.evidence_store = EvidenceStore(self.db_path)
        self.index_root = Path(index_root or self.db_path.parent / "live_index")
        self.embedder = embedder
        self.chunker_version = chunker_version

    # -- helpers ---------------------------------------------------------
    def _scope_dir(self, scope):
        return self.index_root / _safe_component(scope.project_id) / scope.dataset_kind

    def _embedder_instance(self):
        if self.embedder is None:
            from src.retrieval import _get_embedder
            self.embedder = _get_embedder()
        return self.embedder

    @staticmethod
    def _model_name(embedder):
        return str(getattr(embedder, "model_name", None)
                   or getattr(embedder, "_model_name", None)
                   or "BAAI/bge-small-zh-v1.5")

    @staticmethod
    def _scope(scope):
        return EvidenceScope.model_validate(scope)

    # -- lifecycle -------------------------------------------------------
    def status(self, scope):
        scope = self._scope(scope)
        active = self.store.active_version(scope)
        revision = self.evidence_store.revision(scope)
        stale = bool(active and active["source_revision"] != revision)
        return {
            "project_id": scope.project_id, "dataset_kind": scope.dataset_kind,
            "evidence_revision": revision,
            "active": active,
            "stale": stale,
            "versions": self.store.list_versions(scope),
        }

    def recover(self, scope):
        """Mark interrupted builds failed and remove their temporary folders."""
        scope = self._scope(scope)
        failed = self.store.mark_interrupted_builds_failed()
        removed = []
        base = self._scope_dir(scope)
        if base.is_dir():
            for path in base.glob(".building-*"):
                shutil.rmtree(path, ignore_errors=True)
                removed.append(str(path))
        return {"failed_versions": failed, "removed_temp_dirs": removed}

    def build(self, scope, *, force=False):
        scope = self._scope(scope)
        if scope.dataset_kind != "live":
            raise ValueError("Live indexes are built for dataset_kind='live' only")
        revision = self.evidence_store.revision(scope)
        active = self.store.active_version(scope)
        if (active and not force and active["status"] == "ready"
                and active["source_revision"] == revision):
            return {"status": "up_to_date", "version": active, "stale": False}

        evidence = self.evidence_store.list(scope)
        chunks = []
        for record in evidence:
            chunks.extend(build_chunks(record, chunker_version=self.chunker_version))
        if not chunks:
            raise NoIndexableEvidence(
                f"No indexable Evidence for {scope.project_id}/{scope.dataset_kind}"
            )
        self.store.save_chunks(chunks)

        version_id = "idx_" + hashlib.sha256(
            f"{scope.project_id}|{scope.dataset_kind}|{revision}|{self.chunker_version}|"
            f"{utcnow()}|{uuid.uuid4().hex}".encode("utf-8")
        ).hexdigest()[:24]
        scope_dir = self._scope_dir(scope)
        scope_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = scope_dir / f".building-{version_id}"
        final_dir = scope_dir / version_id
        temp_dir.mkdir()
        started = False
        try:
            embedder = self._embedder_instance()
            # Embed the stable record identifier together with the chunk text so
            # exact-ID queries can reach the correct parent record.  The chunk
            # ``text`` used for citations remains unchanged.
            texts = [chunk.get("retrieval_text") or chunk["text"] for chunk in chunks]
            vectors = []
            for start in range(0, len(texts), 64):
                batch = embedder.encode(texts[start:start + 64],
                                        normalize_embeddings=True)
                vectors.append(np.asarray(batch, dtype=np.float32))
            embeddings = np.vstack(vectors).astype(np.float32)
            if embeddings.ndim != 2 or len(embeddings) != len(chunks):
                raise ValueError("Embedding output does not match chunk count")
            dim = int(embeddings.shape[1])
            model_name = self._model_name(embedder)

            self.store.start_version(
                scope, version_id=version_id, embedding_model=model_name,
                embedding_dim=dim, chunker_version=self.chunker_version,
                source_revision=revision, evidence_count=len(evidence),
                chunk_count=len(chunks), path=str(final_dir),
            )
            started = True

            import faiss  # lazy import after the embedding model is initialized
            index = faiss.IndexFlatIP(dim)
            index.add(embeddings)
            faiss_path = temp_dir / "faiss.index"
            faiss.write_index(index, str(faiss_path))
            chunks_path = temp_dir / "chunks.jsonl"
            with open(chunks_path, "w", encoding="utf-8", newline="\n") as handle:
                for chunk in chunks:
                    handle.write(json.dumps(chunk, ensure_ascii=False, sort_keys=True) + "\n")
            faiss_sha = _sha256_file(faiss_path)
            chunks_sha = _sha256_file(chunks_path)
            manifest = {
                "schema_version": 2,
                "index_version": version_id,
                "project_id": scope.project_id,
                "dataset_kind": scope.dataset_kind,
                "embedding_model": model_name,
                "embedding_dim": dim,
                "chunker_version": self.chunker_version,
                "source_revision": revision,
                "evidence_count": len(evidence),
                "chunk_count": len(chunks),
                "files": {"faiss.index": faiss_sha, "chunks.jsonl": chunks_sha},
                "created_at": utcnow(),
            }
            (temp_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_dir, final_dir)
            version = self.store.publish(
                version_id, faiss_sha256=faiss_sha, chunks_sha256=chunks_sha,
                chunk_ids=chunks, expected_revision=revision,
            )
            return {"status": "built", "version": version, "stale": False,
                    "manifest": manifest}
        except Exception as exc:
            if started:
                self.store.fail_version(version_id, {
                    "type": type(exc).__name__, "message": str(exc)})
            shutil.rmtree(temp_dir, ignore_errors=True)
            if final_dir.exists() and not self.store.get_version(version_id):
                shutil.rmtree(final_dir, ignore_errors=True)
            raise

    def verify(self, scope, version_id=None):
        scope = self._scope(scope)
        version = (self.store.get_version(version_id) if version_id
                   else self.store.active_version(scope))
        if version is None:
            return {"ok": False, "reason": "index_not_found", "version": None}
        base = Path(version["path"])
        manifest_path = base / "manifest.json"
        if not manifest_path.is_file():
            return {"ok": False, "reason": "manifest_missing", "version": version}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        problems = []
        if manifest.get("source_revision") != version["source_revision"]:
            problems.append("manifest_revision_mismatch")
        for name, expected in manifest.get("files", {}).items():
            path = base / name
            if not path.is_file():
                problems.append(f"missing:{name}")
            elif _sha256_file(path) != expected:
                problems.append(f"checksum:{name}")
        if version["source_revision"] != self.evidence_store.revision(scope):
            problems.append("stale_source_revision")
        return {"ok": not problems, "problems": problems, "version": version,
                "manifest": manifest}
