"""Small transport objects shared by source connectors and SyncService."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceSpec:
    project_id: str
    dataset_kind: str
    connector_type: str
    source_uri: str
    source_id: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    display_name: str | None = None


@dataclass
class ScanBatch:
    records: list[dict] = field(default_factory=list)
    seen_ids: set[str] = field(default_factory=set)
    # Records no longer present.  Never inferred unless the scan is complete.
    deletions: list[str] = field(default_factory=list)
    # Git commits no longer reachable after a rewrite; retained for audit.
    orphaned: list[str] = field(default_factory=list)
    next_cursor: dict[str, Any] = field(default_factory=dict)
    complete: bool = True
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


@dataclass
class SyncReport:
    source_id: str
    sync_id: str
    status: str
    mode: str
    scanned: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    orphaned: int = 0
    failed: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    revision_before: int = 0
    revision_after: int = 0
    cursor_after: dict[str, Any] = field(default_factory=dict)

    def as_dict(self):
        return {
            "source_id": self.source_id,
            "sync_id": self.sync_id,
            "status": self.status,
            "mode": self.mode,
            "scanned": self.scanned,
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "orphaned": self.orphaned,
            "failed": self.failed,
            "skipped": self.skipped,
            "warnings": list(self.warnings),
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "cursor_after": self.cursor_after,
        }
