"""
Real data ingestion pipeline.

Connects to real data sources and normalizes them into the unified
document format consumed by the retrieval engine.

Supported sources:
  - Git history     → structured commit records
  - Local Markdown / plain text → complete file evidence
  - CSV / TSV       → row → record mapping
  - Directory scan  → supported UTF-8 file types, stable relative-path identity

Usage:
    python -m src.ingest --git /path/to/repo --project-id team-project --dry-run
    python -m src.ingest --dir /path/to/docs --dir-category document --project-id team-project
    python -m src.ingest --csv /path/to/chats.csv --category chat \
        --id-column message_id --time-column event_time --sender-column sender \
        --channel-column channel --subject-column subject --source-ref-column source_ref \
        --project-id team-project
"""

from __future__ import annotations

import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.evidence import Evidence, EvidenceScope
from src.evidence_store import EvidenceStore, default_db_path

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"

# Category is a business label; source_type is the evidence schema field.
# Keep the aliases explicit so generic CSV imports never silently become
# "document" unless the caller actually asked for a document category.
CATEGORY_SOURCE_TYPES = {
    "chat": "chat", "chats": "chat", "message": "chat", "messages": "chat",
    "conversation": "chat", "conversations": "chat",
    "meeting": "meeting", "meetings": "meeting", "minutes": "meeting", "meeting_minutes": "meeting",
    "issue": "issue", "issues": "issue", "ticket": "issue", "tickets": "issue", "bug": "issue", "bugs": "issue",
    "log": "log", "logs": "log",
    "document": "document", "documents": "document", "doc": "document", "docs": "document",
    "requirement": "document", "requirements": "document", "review": "document", "reviews": "document",
    "decision": "document", "decisions": "document", "note": "document", "notes": "document",
    "markdown": "document", "default": "document", "csv_import": "document",
    "code": "code",
}
CATEGORY_HELP = "chat, meeting, issue, document (aliases: chats/minutes/tickets/docs/requirements/reviews/decisions)"


def source_type_for_category(category: str) -> str:
    key = str(category).strip().lower().replace("-", "_")
    try:
        return CATEGORY_SOURCE_TYPES[key]
    except KeyError:
        raise ValueError(f"Unsupported category {category!r}; use {CATEGORY_HELP}") from None


def _parse_event_time(value, *, row_number: int, column: str):
    """Return an aware datetime, or None only when the cell is blank.

    A missing time is preserved as unknown. A non-blank value that is not
    ISO-8601 with an explicit offset is rejected instead of being guessed.
    """
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Invalid {column!r} at CSV row {row_number}: {text!r}; use ISO 8601 with timezone"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"Invalid {column!r} at CSV row {row_number}: {text!r}; timezone is required"
        )
    return parsed


def _structured_content(content: str, fields: list[tuple[str, str]]) -> str:
    """Keep mapped fields visible to BM25 while retaining the original body."""
    header = [f"{label}: {value}" for label, value in fields if value and value.strip()]
    return "\n".join([*header, content]) if header else content


def _parse_assertions(value, *, row_number: int, column: str):
    """Parse an optional JSON object of scalar claims; no prose interpretation."""
    if value is None or not str(value).strip():
        return None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {column!r} at CSV row {row_number}: expected a JSON object") from exc
    if not isinstance(parsed, dict) or not all(
            isinstance(item, (str, int, float, bool)) for item in parsed.values()):
        raise ValueError(f"Invalid {column!r} at CSV row {row_number}: expected scalar values only")
    return parsed


# ---------------------------------------------------------------------------
# Unified record format
# ---------------------------------------------------------------------------

def normalize_record(
    content: str,
    source: str,
    category: str,
    record_id: Optional[str] = None,
    extra_meta: Optional[dict] = None,
    *, project_id: str, source_type: str = "document", event_time=None, locator=None,
) -> dict:
    """Convert any data source to the standard record format.

    This is the single normalization point – all ingest functions
    produce records through this function.
    """
    if record_id is None:
        raise ValueError("A stable original record ID is required")
    scope = EvidenceScope(dataset_kind="live", project_id=project_id)
    evidence = Evidence.create(**scope.model_dump(), source_type=source_type,
                               source_uri=source, original_record_id=record_id,
                               content=content, event_time=event_time,
                               ingested_at=datetime.now(timezone.utc), locator=locator or {},
                               metadata={**(extra_meta or {}), "category": category})
    return evidence.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Git history
# ---------------------------------------------------------------------------

def ingest_git_history(
    repo_path: str | Path,
    days: int | None = None,
    max_commits: int | None = 100,
    *, project_id: str, repository_uri: str | None = None,
    since_commit: str | None = None, branch: str | None = None,
) -> List[dict]:
    """Parse ``git log`` into structured commit records.

    Each record includes: hash, author, date, message, changed files.
    ``since_commit`` enables an incremental ``sha..branch`` range.  The
    default remains the historic 100-commit cap for the legacy CLI; the V2
    connector passes an explicit None for a complete scan.
    """
    repo = Path(repo_path).resolve()
    # Accept worktrees too; use actual repository location, never just basename.
    subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-dir"],
                   capture_output=True, check=True, timeout=10)
    source_uri = repository_uri or repo.as_uri()

    cmd = ["git", "-C", str(repo), "log"]
    if max_commits is not None:
        cmd.append(f"--max-count={max_commits}")
    cmd.append("--format=%H%x00%an%x00%ae%x00%cI%x00%B%x00")
    if days is not None:
        cmd.append(f"--since={days} days ago")
    revision = branch or "HEAD"
    cmd.append(f"{since_commit}..{revision}" if since_commit else revision)
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                            errors="strict", check=True, timeout=30)

    commits = []
    parts = result.stdout.split("\0")
    for offset in range(0, len(parts) - 4, 5):
        commit_hash, author, email, date_str, message = parts[offset:offset + 5]
        commit_hash = commit_hash.strip()
        files = _get_commit_files(repo, commit_hash)

        content = (
            f"commit {commit_hash} by {author}\n"
            f"date: {date_str}\n"
            f"message: {message}\n"
            f"files: {', '.join(files)}"
        )

        commits.append(normalize_record(
            content=content,
            source=source_uri,
            category="commits",
            record_id=commit_hash, project_id=project_id, source_type="git_commit",
            event_time=date_str, locator={"commit_sha": commit_hash, "repository_uri": source_uri, "files": files},
            extra_meta={
                "hash": commit_hash,
                "repository_uri": source_uri,
                "author": author,
                "email": email,
                "date": date_str,
                "message": message,
                "files": files,
            },
        ))

    return commits


def _get_commit_files(repo: Path, commit_hash: str) -> List[str]:
    """Get the list of files changed in a commit."""
    result = subprocess.run(
        ["git", "-C", str(repo), "diff-tree", "--root", "--no-commit-id",
         "--name-only", "-r", "-z", commit_hash],
        capture_output=True, text=True, encoding="utf-8", check=True, timeout=10,
    )
    return [f for f in result.stdout.split("\0") if f]


# ---------------------------------------------------------------------------
# Markdown / plain text directory
# ---------------------------------------------------------------------------

_SECRET_SUFFIXES = {".env", ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der"}
_SECRET_NAMES = {".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "secrets"}


def _is_secret_like(path: Path) -> bool:
    name = path.name.lower()
    return (name in _SECRET_NAMES
            or path.suffix.lower() in _SECRET_SUFFIXES
            or name.startswith(".env.")
            or "credential" in name
            or "secret" in name)


def ingest_directory(
    dir_path: str | Path,
    pattern: str = "*.md",
    recursive: bool = True,
    *, project_id: str, category: Optional[str] = None, source_type: Optional[str] = None,
) -> List[dict]:
    """Scan supported UTF-8 text files, preserving complete original content.

    Parameters
    ----------
    dir_path:
        Root directory to scan.
    pattern:
        Glob pattern (default ``*.md``, use ``*.txt`` or ``*.*`` for all).
    recursive:
        Whether to recurse into subdirectories.
    category:
        Optional business category (e.g. ``meeting``); when supplied it
        overrides extension-based category/source_type detection.
    source_type:
        Optional explicit evidence source type; normally inferred from
        *category*.
    """
    root = Path(dir_path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(str(root))

    glob_fn = root.rglob if recursive else root.glob
    records = []

    for file_path in sorted(glob_fn(pattern)):
        if file_path.is_dir() or file_path.is_symlink() or any(p in {".git", ".venv", "venv", "__pycache__"} for p in file_path.relative_to(root).parts):
            continue
        if _is_secret_like(file_path):
            continue

        # Auto-detect category from extension unless the caller supplied one.
        suffix = file_path.suffix.lower()
        if category:
            record_category = category
            record_source_type = source_type or source_type_for_category(category)
        elif suffix in (".md", ".markdown"):
            record_category, record_source_type = "markdown", "document"
        elif suffix in (".py", ".js", ".ts", ".java", ".go", ".rs"):
            record_category, record_source_type = "code", "code"
        elif suffix in (".log",):
            record_category, record_source_type = "logs", "log"
        elif suffix in (".txt",):
            record_category, record_source_type = "default", "document"
        else:
            continue  # never silently ingest arbitrary binary/secret/config files
        if source_type and not category:
            record_source_type = source_type

        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"Not UTF-8 text: {file_path}")

        if not text:
            continue

        relative_path = file_path.relative_to(root).as_posix()
        # Store the complete original file; retrieval views can chunk later.
        records.append(normalize_record(
            content=text, source=root.as_uri(), category=record_category,
            record_id=relative_path, project_id=project_id,
            source_type=record_source_type,
            locator={"path": relative_path, "file_uri": file_path.as_uri()},
            extra_meta={"event_time_status": "unknown", "time_source": "not_parsed_from_file"},
        ))

    return records


# ---------------------------------------------------------------------------
# CSV / TSV importer
# ---------------------------------------------------------------------------

def ingest_csv(
    csv_path: str | Path,
    content_column: str = "description",
    id_column: Optional[str] = None,
    delimiter: str = ",",
    category: Optional[str] = None,
    *, project_id: str, time_column: Optional[str] = None,
    sender_column: Optional[str] = None, channel_column: Optional[str] = None,
    subject_column: Optional[str] = None, source_ref_column: Optional[str] = None,
    assertions_column: Optional[str] = None,
    source_type: Optional[str] = None,
) -> List[dict]:
    """Import records from a CSV file.

    Each row becomes one record. The *content_column* specifies the main
    text body; mapped fields are also placed in the searchable content so
    BM25 can find sender/subject/channel terms. Additional columns remain
    in metadata under ``columns``.

    Parameters
    ----------
    csv_path:
        Path to a .csv or .tsv file.
    content_column:
        Column name for the main text content.
    id_column:
        Column used as the stable unique record ID. If supplied, every row
        must have a non-blank value; otherwise importing remains row-position
        based and is disclosed in metadata.
    delimiter:
        CSV delimiter (`,` for CSV, `\t` for TSV).
    category:
        Required business category: chat, meeting, issue, document (aliases
        accepted). The category determines ``source_type`` unless *source_type*
        overrides. There is intentionally no silent "everything is document"
        default.
    time_column:
        Optional event-time column; blank means unknown, non-blank must be
        ISO-8601 with an explicit timezone.
    sender_column / channel_column / subject_column / source_ref_column:
        Optional fields preserved in metadata and included in searchable content.
    assertions_column:
        Optional JSON-object column with machine-readable scalar claims. These
        are kept in ``metadata.assertions`` for deterministic contradiction
        checks; they are never interpreted as proof by prose.
    source_type:
        Optional explicit evidence source type; normally inferred from category.
    """
    path = Path(csv_path).resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    if category is None or not str(category).strip():
        raise ValueError(f"CSV import requires an explicit category; use {CATEGORY_HELP}")

    resolved_source_type = source_type or source_type_for_category(category)
    records = []
    seen_ids = set()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        fieldnames = set(reader.fieldnames or [])
        required = [content_column] + ([id_column] if id_column else []) + ([time_column] if time_column else [])
        if assertions_column:
            required.append(assertions_column)
        missing_columns = [column for column in required if column not in fieldnames]
        if missing_columns:
            raise ValueError(f"CSV {path.name} is missing required column(s): {missing_columns}")

        for ri, row in enumerate(reader):
            if not row:
                continue

            # Build content from the primary column
            content = row.get(content_column, "")
            if isinstance(content, list):
                content = " | ".join(str(v) for v in content)

            # Fall back to joining all non-empty columns
            if not content.strip():
                content = " | ".join(
                    f"{k}: {v}" for k, v in row.items() if v and k.strip()
                )

            if not content.strip():
                continue

            raw_id = row.get(id_column) if id_column else None
            if id_column and (raw_id is None or not str(raw_id).strip()):
                raise ValueError(f"Missing {id_column!r} at CSV row {ri + 2}")
            record_id = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else f"row:{ri + 2}"
            if record_id in seen_ids:
                raise ValueError(f"Duplicate record ID {record_id!r} in {path.name}")
            seen_ids.add(record_id)

            raw_time = row.get(time_column) if time_column else None
            event_time = _parse_event_time(raw_time, row_number=ri + 2, column=time_column or "time_column")
            sender = row.get(sender_column) if sender_column else None
            channel = row.get(channel_column) if channel_column else None
            subject = row.get(subject_column) if subject_column else None
            source_ref = row.get(source_ref_column) if source_ref_column else None
            assertions = _parse_assertions(row.get(assertions_column) if assertions_column else None,
                                           row_number=ri + 2, column=assertions_column or "assertions_column")
            content = _structured_content(content.strip(), [
                ("sender", sender or ""), ("channel", channel or ""),
                ("subject", subject or ""), ("source_ref", source_ref or ""),
            ])

            locator = {"csv_row": ri + 2, "csv_path": path.as_uri()}
            if source_ref and source_ref.strip():
                locator["source_ref"] = source_ref.strip()
            extra_meta = {
                "columns": row,
                "identity_basis": "column" if id_column else "row_position",
                "structured_import": True,
                "event_time_status": "recorded" if event_time else "unknown",
                "event_time_raw": (raw_time or "").strip() or None,
                "sender": sender or None,
                "channel": channel or None,
                "subject": subject or None,
                "source_ref": source_ref or None,
            }
            if assertions is not None:
                extra_meta["assertions"] = assertions
            records.append(normalize_record(
                content=content,
                source=path.as_uri(),
                category=category,
                record_id=record_id, project_id=project_id,
                source_type=resolved_source_type,
                event_time=event_time,
                locator=locator,
                extra_meta=extra_meta,
            ))

    return records


# ---------------------------------------------------------------------------
# JSON / JSONL structured records
# ---------------------------------------------------------------------------

def _nested_get(record: dict, path: str | None):
    if not path:
        return record
    value = record
    for part in str(path).split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _flatten_json(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " | ".join(_flatten_json(item) for item in value)
    if isinstance(value, dict):
        return " | ".join(f"{key}: {_flatten_json(item)}" for key, item in value.items())
    return str(value)


def _load_json_records(path: Path, record_path: str | None):
    """Return ``(record, locator, line_number)`` tuples; never executes data."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        records = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path.name} line {number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL record at {path.name} line {number} must be an object")
            records.append((value, {"line": number}, number))
        return records
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path.name}") from exc
    if record_path:
        value = _nested_get(value, record_path)
    elif isinstance(value, dict):
        for key in ("records", "items", "data", "issues", "commits", "logs", "messages"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            value = [value]
    if not isinstance(value, list):
        raise ValueError(f"JSON {path.name} must contain an array of records or a configured record_path")
    records = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"JSON record {index} in {path.name} must be an object")
        records.append((item, {"json_index": index}, index + 1))
    return records


def ingest_json_records(
    json_path: str | Path,
    *,
    project_id: str,
    category: str,
    record_path: str | None = None,
    id_field: str = "id",
    content_fields: list[str] | None = None,
    time_field: str | None = None,
    source_type: str | None = None,
) -> List[dict]:
    """Import JSON arrays, configured JSON object paths, or JSONL lines.

    A stable ID field is required.  Row-position fallback would make deletion
    reconciliation unsafe, so it is deliberately rejected here.
    """
    path = Path(json_path).resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    if not category or not str(category).strip():
        raise ValueError("JSON import requires an explicit category")
    resolved_source_type = source_type or source_type_for_category(category)
    records = []
    seen_ids = set()
    for record, locator_extra, ordinal in _load_json_records(path, record_path):
        raw_id = _nested_get(record, id_field)
        if raw_id is None or not str(raw_id).strip():
            raise ValueError(f"Missing JSON ID field {id_field!r} at record {ordinal} in {path.name}")
        record_id = str(raw_id).strip()
        if record_id in seen_ids:
            raise ValueError(f"Duplicate JSON record ID {record_id!r} in {path.name}")
        seen_ids.add(record_id)

        if content_fields:
            body = "\n".join(f"{field}: {_flatten_json(_nested_get(record, field))}"
                             for field in content_fields
                             if _nested_get(record, field) not in (None, ""))
        else:
            body = ""
            for key in ("content", "message", "description", "title", "summary", "text"):
                value = record.get(key)
                if value not in (None, ""):
                    body = _flatten_json(value)
                    break
            if not body:
                body = _flatten_json(record)
        if not body.strip():
            body = _flatten_json(record)
        if not body.strip():
            continue

        raw_time = _nested_get(record, time_field) if time_field else None
        event_time = _parse_event_time(raw_time, row_number=ordinal,
                                       column=time_field or "time_field")
        locator = {"json_path": path.as_uri(), **locator_extra}
        extra_meta = {
            "json_record": record,
            "identity_basis": "field",
            "structured_import": True,
            "source_format": "jsonl" if path.suffix.lower() in {".jsonl", ".ndjson"} else "json",
            "event_time_status": "recorded" if event_time else "unknown",
            "event_time_raw": str(raw_time).strip() if raw_time not in (None, "") else None,
        }
        records.append(normalize_record(
            content=body.strip(), source=path.as_uri(), category=category,
            record_id=record_id, project_id=project_id,
            source_type=resolved_source_type, event_time=event_time,
            locator=locator, extra_meta=extra_meta,
        ))
    return records


# ---------------------------------------------------------------------------
# Full ingestion orchestrator
# ---------------------------------------------------------------------------

def ingest_all(
    repo_path: Optional[str | Path] = None,
    docs_dir: Optional[str | Path] = None,
    csv_files: Optional[List[str | Path | dict]] = None,
    output_dir: str | Path = LIVE_DIR,
    reindex: bool = False,
    *, project_id: str, db_path=None, dry_run: bool = False,
    docs_category: Optional[str] = None, docs_source_type: Optional[str] = None,
) -> Dict[str, int]:
    """Run all available ingestion pipelines.

    Parameters
    ----------
    repo_path:
        Git repo to ingest commits from.
    docs_dir:
        Directory of Markdown/text files to ingest.
    csv_files:
        CSV files to import. Each item may be a path, or a dict with ``path``
        plus per-file options: ``content_column``, ``id_column``, ``delimiter``,
        ``time_column``, ``sender_column``, ``channel_column``,
        ``subject_column``, ``source_ref_column``, ``assertions_column``,
        ``source_type``, ``name``.
        ``category`` is required for every CSV specification.
    output_dir:
        Directory used for the default live SQLite database path.
    reindex:
        Deprecated: True is rejected to protect existing indexes.
    docs_category / docs_source_type:
        Optional category/source-type override for directory ingestion.

    Returns
    -------
    dict
        ``{source_name: record_count}`` for each pipeline that ran.
    """
    if reindex:
        raise ValueError("Destructive reindex is disabled; live retrieval reads Evidence directly")
    EvidenceScope(dataset_kind="live", project_id=project_id)
    output = Path(output_dir)
    counts: Dict[str, int] = {}
    all_records: List[dict] = []

    # 1. Git history
    if repo_path:
        print(f"[1/4] Ingesting git history from {repo_path}...")
        commits = ingest_git_history(repo_path, project_id=project_id)
        if commits:
            all_records.extend(commits)
            counts["git_commits"] = len(commits)
            print(f"      → {len(commits)} commits")

    # 2. Markdown / text directory
    if docs_dir:
        print(f"[2/4] Scanning directory {docs_dir}...")
        docs = ingest_directory(docs_dir, pattern="*.*", project_id=project_id,
                                category=docs_category, source_type=docs_source_type)
        if docs:
            all_records.extend(docs)
            counts["directory_files"] = len({d["original_record_id"] for d in docs})
            counts["directory_chunks"] = len(docs)
            print(f"      → {counts['directory_files']} files → {counts['directory_chunks']} chunks")

    # 3. CSV imports
    if csv_files:
        print(f"[3/4] Importing {len(csv_files)} CSV file(s)...")
        for cf in csv_files:
            spec = dict(cf) if isinstance(cf, dict) else {}
            csv_path = spec.pop("path", cf)
            label = str(spec.pop("name", Path(csv_path).stem))
            if not isinstance(csv_path, (str, Path)):
                raise TypeError("CSV spec requires a path")
            csv_records = ingest_csv(
                csv_path,
                content_column=spec.pop("content_column", "description"),
                id_column=spec.pop("id_column", None),
                delimiter=spec.pop("delimiter", ","),
                category=spec.pop("category", None),
                project_id=project_id,
                time_column=spec.pop("time_column", None),
                sender_column=spec.pop("sender_column", None),
                channel_column=spec.pop("channel_column", None),
                subject_column=spec.pop("subject_column", None),
                source_ref_column=spec.pop("source_ref_column", None),
                assertions_column=spec.pop("assertions_column", None),
                source_type=spec.pop("source_type", None),
            )
            if spec:
                raise ValueError(f"Unknown CSV spec option(s): {sorted(spec)}")
            if csv_records:
                all_records.extend(csv_records)
                counts[f"csv_{label}"] = len(csv_records)
                print(f"      → {Path(csv_path).name}: {len(csv_records)} records")

    # Append a batch atomically; never overwrite legacy JSON or rebuild FAISS.
    if all_records:
        counts["total_records"] = len(all_records)
        if not dry_run:
            store = EvidenceStore(db_path or (default_db_path() if output == LIVE_DIR else output / "evidence.sqlite3"))
            states = [{"project_id": project_id, "dataset_kind": "live", "source_uri": source,
                       "observed_ids": [r["original_record_id"] for r in all_records if r["source_uri"] == source]}
                      for source in sorted({r["source_uri"] for r in all_records})]
            counts["appended_observations"] = store.append(all_records, states)
        else:
            counts["appended_observations"] = 0

    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Append live evidence; never replace JSON or rebuild FAISS")
    parser.add_argument("--git", dest="repo")
    parser.add_argument("--dir", dest="docs")
    parser.add_argument("--dir-category", help=f"directory category, e.g. meeting/document; {CATEGORY_HELP}")
    parser.add_argument("--dir-source-type")
    parser.add_argument("--csv", action="append", default=[])
    parser.add_argument("--category", help=f"CSV business category; required with --csv; {CATEGORY_HELP}")
    parser.add_argument("--content-column", default="description")
    parser.add_argument("--id-column")
    parser.add_argument("--time-column")
    parser.add_argument("--sender-column")
    parser.add_argument("--channel-column")
    parser.add_argument("--subject-column")
    parser.add_argument("--source-ref-column")
    parser.add_argument("--delimiter", default=",")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not any([args.repo, args.docs, args.csv]):
        parser.error("provide --git, --dir or --csv")
    if args.csv and not args.category:
        parser.error("--category is required with --csv; do not silently import everything as document")
    csv_files = [{
        "path": path, "category": args.category, "content_column": args.content_column,
        "id_column": args.id_column, "delimiter": args.delimiter,
        "time_column": args.time_column, "sender_column": args.sender_column,
        "channel_column": args.channel_column, "subject_column": args.subject_column,
        "source_ref_column": args.source_ref_column,
    } for path in args.csv]
    counts = ingest_all(repo_path=args.repo, docs_dir=args.docs, csv_files=csv_files,
                        project_id=args.project_id, db_path=args.db, dry_run=args.dry_run,
                        docs_category=args.dir_category, docs_source_type=args.dir_source_type)
    print(f"\nDone. {counts}")
