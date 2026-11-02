"""Evidence identity and provenance. Retrieval scores are intentionally external."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEMO_PROJECT = "collaborative-memory-demo"


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def stable_evidence_id(project_id, dataset_kind, source_type, source_uri, original_record_id):
    identity = json.dumps([project_id, dataset_kind, source_type, source_uri,
                           original_record_id], ensure_ascii=False, separators=(",", ":"))
    return "ev_" + sha256_text(identity)


class EvidenceScope(BaseModel):
    """A query partition, never inferred from the natural-language question."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset_kind: Literal["fixture", "live"] = "fixture"
    project_id: str = Field(default=DEMO_PROJECT, min_length=1, max_length=200)

    @field_validator("project_id")
    @classmethod
    def valid_project(cls, value):
        if value != value.strip() or not value.strip():
            raise ValueError("project_id must be nonblank without surrounding whitespace")
        return value

    @model_validator(mode="before")
    @classmethod
    def explicit_live_project(cls, values):
        if isinstance(values, dict) and values.get("dataset_kind") == "live" and not values.get("project_id"):
            raise ValueError("live queries require an explicit project_id")
        return values


class Evidence(BaseModel):
    """One original record. Content revisions retain identity; timestamps stay honest."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_id: str = Field(pattern=r"^ev_[0-9a-f]{64}$")
    project_id: str = Field(min_length=1, max_length=200)
    source_type: Literal["git_commit", "issue", "document", "meeting", "chat", "log", "code", "memory", "unknown"]
    source_uri: str | None
    original_record_id: str | None
    event_time: datetime | None = None
    ingested_at: datetime | None = None
    content: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    locator: dict[str, Any] = Field(default_factory=dict)
    dataset_kind: Literal["fixture", "live"]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("project_id", "original_record_id")
    @classmethod
    def nonblank(cls, value):
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("identity fields must be nonblank without surrounding whitespace")
        return value

    @field_validator("source_uri")
    @classmethod
    def absolute_uri(cls, value):
        if value is not None and (not urlsplit(value).scheme or any(c.isspace() for c in value)):
            raise ValueError("source_uri must be an absolute URI without whitespace")
        return value

    @field_validator("event_time", "ingested_at")
    @classmethod
    def aware_time(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("timestamps require an explicit timezone")
        return value

    @model_validator(mode="after")
    def integrity(self):
        if sha256_text(self.content) != self.content_hash:
            raise ValueError("content_hash does not match content")
        identity_record = self.original_record_id or self.metadata.get("legacy_identity_key")
        expected = stable_evidence_id(self.project_id, self.dataset_kind, self.source_type,
                                      self.source_uri, identity_record)
        if self.evidence_id != expected:
            raise ValueError("evidence_id does not match identity")
        missing = [key for key in ("source_uri", "original_record_id", "event_time", "ingested_at")
                   if getattr(self, key) is None]
        if missing and not self.metadata.get("legacy"):
            # Event time is legitimately absent for files; collection time is not.
            if any(key != "event_time" for key in missing):
                raise ValueError("new evidence requires source URI, original ID and ingestion time")
        if self.metadata.get("legacy") and not set(missing).issubset(self.metadata.get("missing_fields", [])):
            raise ValueError("legacy records must disclose missing provenance")
        if self.source_type == "git_commit" and not self.metadata.get("legacy"):
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.original_record_id or ""):
                raise ValueError("Git evidence requires the full commit SHA")
            if self.locator.get("commit_sha") != self.original_record_id or self.locator.get("repository_uri") != self.source_uri:
                raise ValueError("Git locator must match commit identity and repository")
            if self.event_time is None:
                raise ValueError("Git evidence requires its original commit timestamp")
        return self

    @classmethod
    def create(cls, *, project_id, dataset_kind, source_type, source_uri,
               original_record_id, content, **kwargs):
        identity = original_record_id or kwargs.get("metadata", {}).get("legacy_identity_key")
        return cls(evidence_id=stable_evidence_id(project_id, dataset_kind, source_type, source_uri, identity),
                   project_id=project_id, dataset_kind=dataset_kind, source_type=source_type,
                   source_uri=source_uri, original_record_id=original_record_id, content=content,
                   content_hash=sha256_text(content), **kwargs)

    def as_hit(self, score=0.0, source=None):
        """Legacy aliases supplement, never replace, the canonical fields."""
        return {**self.model_dump(mode="json"), "id": self.evidence_id,
                "source": source or self.source_uri or "legacy:unknown",
                "record": self.original_record_id or self.evidence_id,
                "score": float(score), "score_kind": "retrieval_relevance"}


def legacy_evidence(item: dict, *, scope: EvidenceScope | None = None) -> Evidence:
    """Wrap legacy snippets without claiming their text is original-system evidence.

    Legacy source labels are claims, not verified URIs. Their file/record position
    is retained in metadata. A fallback content digest is explicitly synthetic.
    """
    if "evidence_id" in item:
        return Evidence.model_validate({k: item[k] for k in Evidence.model_fields if k in item})
    scope = scope or EvidenceScope()
    source = item.get("source") or (item.get("metadata") or {}).get("source")
    record = item.get("record", item.get("paragraph"))
    identity = json.dumps([source, record if record is not None else sha256_text(item["content"])],
                          ensure_ascii=False, separators=(",", ":"))
    return Evidence.create(
        **scope.model_dump(), source_type="unknown", source_uri=None,
        original_record_id=None, content=item["content"], locator={},
        metadata={"legacy": True, "legacy_identity_key": identity,
                  "legacy_source_label": source, "legacy_record_locator": record,
                  "missing_fields": ["source_uri", "original_record_id", "event_time", "ingested_at"],
                  "provenance_status": "unverified_legacy", "legacy_metadata": item.get("metadata", {})},
    )
