"""Real source connectors.  They scan only; SyncService owns database writes."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from src.ingest import ingest_csv, ingest_directory, ingest_git_history, ingest_json_records
from src.sync.models import ScanBatch, SourceSpec


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git(repo: Path, *args, timeout=20):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=True, timeout=timeout).stdout.strip()


class GitConnector:
    connector_type = "git"

    def scan(self, source: SourceSpec, cursor: dict | None, known_ids: set[str]) -> ScanBatch:
        repo = Path(source.config.get("repo_path") or "").resolve()
        if not repo.is_dir():
            raise FileNotFoundError(f"Git repository not found: {repo}")
        branch = source.config.get("branch") or "main"
        max_commits = source.config.get("max_commits")
        warnings: list[str] = []
        mode = "full"
        since = None
        shallow = _git(repo, "rev-parse", "--is-shallow-repository") == "true"
        try:
            head = _git(repo, "rev-parse", branch)
        except subprocess.CalledProcessError:
            branch = "HEAD"
            head = _git(repo, "rev-parse", "HEAD")
        last = (cursor or {}).get("last_commit_sha")
        if last:
            exists = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{last}^{{commit}}"],
                                    capture_output=True).returncode == 0
            if not exists:
                warnings.append("cursor_invalid_missing_commit")
            else:
                ancestor = subprocess.run(
                    ["git", "-C", str(repo), "merge-base", "--is-ancestor", last, head],
                    capture_output=True).returncode == 0
                if ancestor:
                    mode = "incremental"
                    since = last
                else:
                    warnings.append("history_rewritten_cursor_invalid")
        records = ingest_git_history(
            repo, project_id=source.project_id, repository_uri=source.source_uri,
            max_commits=max_commits, since_commit=since, branch=branch,
        )
        seen = {record["original_record_id"] for record in records}
        # Incremental scans only see new commits; never infer deletions from that.
        complete = mode == "full" and not shallow
        orphaned: list[str] = []
        if mode == "full" and complete and known_ids:
            orphaned = sorted(known_ids - seen)
            if orphaned:
                warnings.append("unreachable_commits_marked_orphaned")
        if shallow:
            warnings.append("shallow_clone_history_incomplete")
        if max_commits is not None and len(records) >= int(max_commits):
            warnings.append("commit_history_truncated")
            complete = False
        next_cursor = {
            "kind": "git", "branch": branch, "last_commit_sha": head,
            "last_commit_time": _git(repo, "show", "-s", "--format=%cI", head),
            "history_mode": "incremental" if mode == "incremental" else "full",
            "shallow": shallow,
            "truncated": max_commits is not None and len(records) >= int(max_commits),
        }
        return ScanBatch(records=records, seen_ids=seen, orphaned=orphaned,
                         next_cursor=next_cursor, complete=complete,
                         warnings=warnings,
                         stats={"commits": len(records)})


class DirectoryConnector:
    connector_type = "directory"

    def scan(self, source: SourceSpec, cursor: dict | None, known_ids: set[str]) -> ScanBatch:
        root = Path(source.config.get("root_path") or "").resolve()
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        records = ingest_directory(
            root, pattern=source.config.get("pattern") or "*.*",
            recursive=source.config.get("recursive", True),
            project_id=source.project_id,
            category=source.config.get("category"),
            source_type=source.config.get("source_type"),
        )
        seen = {record["original_record_id"] for record in records}
        files = {record["original_record_id"]: record["content_hash"] for record in records}
        deletions = []
        warnings = []
        allow_deletes = source.config.get("allow_deletes", True)
        if cursor and allow_deletes:
            deletions = sorted(known_ids - seen)
        elif cursor and not allow_deletes:
            warnings.append("deletion_reconciliation_disabled")
        next_cursor = {
            "kind": "directory", "root_uri": source.source_uri,
            "last_scan_completed_at": datetime.now(timezone.utc).isoformat(),
            "files": files,
        }
        return ScanBatch(records=records, seen_ids=seen, deletions=deletions,
                         next_cursor=next_cursor, complete=True, warnings=warnings,
                         stats={"files": len(records)})


class CsvConnector:
    connector_type = "csv"

    def scan(self, source: SourceSpec, cursor: dict | None, known_ids: set[str]) -> ScanBatch:
        path = Path(source.config.get("path") or "").resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        file_hash = _sha256_bytes(path.read_bytes())
        config = source.config
        id_column = config.get("id_column")
        if cursor and cursor.get("file_sha256") == file_hash:
            return ScanBatch(records=[], seen_ids=set(known_ids), deletions=[],
                             next_cursor={**cursor, "last_checked_at": datetime.now(timezone.utc).isoformat()},
                             complete=True, stats={"files": 0})
        delimiter = config.get("delimiter") or ("\t" if path.suffix.lower() == ".tsv" else ",")
        records = ingest_csv(
            path, content_column=config.get("content_column", "description"),
            id_column=id_column, delimiter=delimiter,
            category=config.get("category"), project_id=source.project_id,
            time_column=config.get("time_column"),
            sender_column=config.get("sender_column"),
            channel_column=config.get("channel_column"),
            subject_column=config.get("subject_column"),
            source_ref_column=config.get("source_ref_column"),
            assertions_column=config.get("assertions_column"),
            source_type=config.get("source_type"),
        )
        seen = {record["original_record_id"] for record in records}
        warnings = []
        deletions = []
        if id_column:
            if cursor:
                deletions = sorted(known_ids - seen)
        else:
            warnings.append("row_position_identity_no_delete_detection")
        next_cursor = {
            "kind": "csv", "file_uri": source.source_uri, "file_sha256": file_hash,
            "id_column": id_column, "delimiter": delimiter, "row_count": len(records),
            "known_ids": sorted(seen),
            "schema_hash": _sha256_bytes("\0".join(sorted((records[0].get("metadata", {}).get("columns") or {}).keys())).encode()) if records else "",
        }
        return ScanBatch(records=records, seen_ids=seen, deletions=deletions,
                         next_cursor=next_cursor, complete=True, warnings=warnings,
                         stats={"rows": len(records)})


class JsonConnector:
    connector_type = "json"

    def scan(self, source: SourceSpec, cursor: dict | None, known_ids: set[str]) -> ScanBatch:
        path = Path(source.config.get("path") or "").resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        file_hash = _sha256_bytes(path.read_bytes())
        config = source.config
        if cursor and cursor.get("file_sha256") == file_hash:
            return ScanBatch(records=[], seen_ids=set(known_ids), deletions=[],
                             next_cursor={**cursor, "last_checked_at": datetime.now(timezone.utc).isoformat()},
                             complete=True, stats={"records": 0})
        records = ingest_json_records(
            path, project_id=source.project_id, category=config.get("category"),
            record_path=config.get("record_path"), id_field=config.get("id_field", "id"),
            content_fields=config.get("content_fields"), time_field=config.get("time_field"),
            source_type=config.get("source_type"),
        )
        seen = {record["original_record_id"] for record in records}
        deletions = sorted(known_ids - seen) if cursor and source.config.get("allow_deletes", True) else []
        next_cursor = {
            "kind": "json", "file_uri": source.source_uri, "file_sha256": file_hash,
            "known_ids": sorted(seen),
            "format": "jsonl" if path.suffix.lower() in {".jsonl", ".ndjson"} else "json",
        }
        return ScanBatch(records=records, seen_ids=seen, deletions=deletions,
                         next_cursor=next_cursor, complete=True,
                         stats={"records": len(records)})


CONNECTORS = {
    "git": GitConnector(),
    "directory": DirectoryConnector(),
    "csv": CsvConnector(),
    "json": JsonConnector(),
}
