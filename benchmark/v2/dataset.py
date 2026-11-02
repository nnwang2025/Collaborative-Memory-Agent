"""Synthetic V2 development corpus.  Frozen Benchmark V1 is never touched."""

from __future__ import annotations

import csv
import json
import os
import subprocess
from pathlib import Path

ARCHITECTURE = """# Architecture Decision Records

## Memory model
ARCH-42 defines SQLite tombstones for every deleted evidence record. The original
evidence observation remains append-only, while evidence_current points at the
latest valid version. A tombstone never deletes history.

## Hybrid retrieval
The retrieval pipeline combines BM25 exact matching with dense vectors from
bge-small. Reciprocal rank fusion merges channel ranks. Every chunk hit is
hydrated to its full parent Evidence before an agent may cite it. Scope checks
reject records from another project or dataset.

## Deployment rollback
Deployment rollback procedure: freeze ingestion, rebuild the versioned live
index from the current evidence snapshot, verify manifest checksums, and switch
the active alias atomically. If the build fails, keep serving the previous
index and use BM25 fallback.

## Time and conflict handling
Event times use timezone-aware ISO-8601. Conflicting proposals remain visible
until a local reviewer records a decision. Current state is never inferred from
file modification time.
"""

CONFLICT_A = """# Deployment proposal A
Proposal: deploy the search service on Friday with the hybrid retrieval index.
The platform team approved the release after reviewing the rollback procedure.
"""

CONFLICT_B = """# Deployment proposal B
Proposal: postpone the search deployment because the dense index build is
unstable. The review team opposes Friday deployment until checksums pass.
"""

OBSOLETE = """# Obsolete retention policy
The obsolete retention policy said to purge evidence after seven days. This
document is removed during the deletion phase of the benchmark.
"""

OTHER_PROJECT = """# Other project notes
The other project also writes about hybrid retrieval, SQLite tombstones, and
rollback procedures. These records must never contaminate the main project.
"""


def _git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


def _commit(repo, message, files, timestamp):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp}
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD")


def create_dataset(root: Path) -> dict:
    """Create directory, CSV, JSON, JSONL, and a real temporary Git repository."""
    root = Path(root)
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "architecture.md").write_text(ARCHITECTURE, encoding="utf-8")
    (docs / "conflict_a.md").write_text(CONFLICT_A, encoding="utf-8")
    (docs / "conflict_b.md").write_text(CONFLICT_B, encoding="utf-8")
    (docs / "obsolete.md").write_text(OBSOLETE, encoding="utf-8")

    records = root / "records"
    records.mkdir()
    issues = records / "issues.csv"
    with open(issues, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "id", "title", "description", "updated_at"])
        writer.writeheader()
        writer.writerow({"id": "ISSUE-101", "title": "Login",
                         "description": "Safari browser login fails on Apple devices",
                         "updated_at": "2026-02-01T09:00:00+08:00"})
        writer.writerow({"id": "ISSUE-102", "title": "Search",
                         "description": "CJK search misses records for Chinese queries",
                         "updated_at": "2026-02-02T09:00:00+08:00"})

    events = records / "events.json"
    events.write_text(json.dumps({"records": [
        {"id": "JSON-1", "title": "Tombstone lifecycle",
         "body": "Tombstones preserve historical evidence versions.",
         "event_time": "2026-02-10T00:00:00Z"},
        {"id": "JSON-2", "title": "Rollback drill",
         "body": "Rollback drill completed after the deployment freeze.",
         "event_time": "2026-03-05T00:00:00Z"},
    ]}, ensure_ascii=False, indent=2), encoding="utf-8")

    stream = records / "stream.jsonl"
    stream.write_text("\n".join([
        json.dumps({"id": "LOG-1", "message": "hybrid retrieval index rebuild started",
                    "ts": "2026-02-11T00:00:00Z"}),
        json.dumps({"id": "LOG-2", "message": "manifest checksum verified before alias switch",
                    "ts": "2026-02-11T00:05:00Z"}),
    ]) + "\n", encoding="utf-8")

    repo = root / "git_repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "bench@example.invalid")
    _git(repo, "config", "user.name", "V2 Benchmark")
    _git(repo, "branch", "-M", "main")
    commit1 = _commit(repo, "Deploy hybrid retrieval with SQLite tombstones", {
        "service/retrieval.py": "def retrieve():\n    return 'hybrid'\n",
    }, "2026-02-01T00:00:00+00:00")
    commit2 = _commit(repo, "Fix ISSUE-101 Safari login fallback", {
        "service/login.py": "def fallback():\n    return 'safari'\n",
    }, "2026-02-02T00:00:00+00:00")
    commit3 = _commit(repo, "Document rollback and index alias switch", {
        "docs/rollback.md": "Rollback keeps the previous index and uses BM25 fallback.\n",
    }, "2026-02-03T00:00:00+00:00")

    other = root / "other_project"
    other.mkdir()
    (other / "notes.md").write_text(OTHER_PROJECT, encoding="utf-8")

    cases = [
        {"id": "exact_issue", "query": "ISSUE-101",
         "expected": ["ISSUE-101"], "kind": "exact_id"},
        {"id": "exact_commit", "query": commit3,
         "expected": [commit3], "kind": "exact_id"},
        {"id": "semantic_login", "query": "Apple browser cannot log in",
         "expected": ["ISSUE-101"], "kind": "semantic_rewrite"},
        {"id": "cross_source", "query": "tombstone lifecycle evidence",
         "expected": ["architecture.md", "JSON-1"], "kind": "cross_source"},
        {"id": "long_document", "query": "atomic alias switch when build fails previous index",
         "expected": ["architecture.md"], "kind": "long_document"},
        {"id": "time_filter", "query": "rollback drill deployment",
         "expected": ["JSON-2"], "kind": "time_constraint",
         "filters": {"after": "2026-03-01T00:00:00Z"}},
        {"id": "conflict", "query": "Friday deployment proposal hybrid index review",
         "expected": ["conflict_a.md", "conflict_b.md"], "kind": "conflict"},
        {"id": "updated_record", "query": "CJK search fixed exact matching tests",
         "expected": ["ISSUE-102"], "kind": "updated_record"},
        {"id": "cross_project", "query": "hybrid retrieval tombstones rollback",
         "expected": ["architecture.md"], "kind": "cross_project"},
        {"id": "empty", "query": "zzzz-no-match-qqq",
         "expected": [], "kind": "empty_result"},
        {"id": "lexical_wrong", "query": "Safari browser cafeteria menu",
         "expected": [], "kind": "lexically_similar_wrong"},
        {"id": "unrelated_science", "query": "photosynthesis chlorophyll telescope",
         "expected": [], "kind": "completely_unrelated"},
        {"id": "tombstone_lunch", "query": "tombstone cafeteria lunch menu",
         "expected": [], "kind": "lexically_similar_wrong"},
        {"id": "obsolete_before_delete", "query": "obsolete purge evidence seven days",
         "expected": ["obsolete.md"], "kind": "deleted_version"},
    ]
    return {
        "root": root, "docs": docs, "issues_csv": issues, "events_json": events,
        "stream_jsonl": stream, "git_repo": repo, "other_project": other,
        "cases": cases,
        "commits": [commit1, commit2, commit3],
        "updated_issue_row": {
            "id": "ISSUE-102", "title": "Search",
            "description": "CJK search issue is now fixed and covered by exact matching tests",
            "updated_at": "2026-03-10T09:00:00+08:00",
        },
    }


def add_release_commit(dataset: dict):
    """Add one commit after the initial sync to exercise the Git cursor."""
    repo = dataset["git_repo"]
    sha = _commit(repo, "Add V2 benchmark release note", {
        "docs/release.md": "V2 benchmark release after checksum verification.\n",
    }, "2026-02-04T00:00:00+00:00")
    dataset["cases"].append({
        "id": "incremental_commit", "query": "V2 benchmark release note checksum",
        "expected": [sha], "kind": "git_incremental",
    })
    dataset["commits"].append(sha)
    return sha


def apply_update(dataset: dict):
    path = dataset["issues_csv"]
    rows = [
        {"id": "ISSUE-101", "title": "Login",
         "description": "Safari browser login fails on Apple devices",
         "updated_at": "2026-02-01T09:00:00+08:00"},
        dataset["updated_issue_row"],
    ]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "id", "title", "description", "updated_at"])
        writer.writeheader()
        writer.writerows(rows)


def apply_deletion(dataset: dict):
    path = dataset["docs"] / "obsolete.md"
    if path.exists():
        path.unlink()
