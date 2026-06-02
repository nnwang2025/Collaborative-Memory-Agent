"""
Real data ingestion pipeline.

Ingests data from real sources (Git history, local file system) and
converts them into structured records compatible with the existing
retrieval pipeline.

Currently supported sources:
  - Git log (local repository)
  - Local Python project files (already handled by DependencySkill)
  - Markdown docs (already handled by DocumentSkill)

Usage:
    python -m src.ingest --git /path/to/repo
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Git ingestion
# ---------------------------------------------------------------------------

def ingest_git_history(
    repo_path: str | Path,
    days: int = 90,
    max_commits: int = 100,
) -> List[dict]:
    """Parse ``git log`` into structured commit records.

    Each record has:
      - hash, author, date, message
      - files: list of changed files
      - summary: auto-generated one-line summary

    Parameters
    ----------
    repo_path:
        Path to a git repository.
    days:
        How far back to look (default 90 days).
    max_commits:
        Maximum number of commits to return.
    """
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        raise FileNotFoundError(f"Not a git repository: {repo}")

    cmd = [
        "git", "-C", str(repo), "log",
        f"--since={days} days ago",
        f"--max-count={max_commits}",
        "--pretty=format:%H|%an|%ae|%ad|%s",
        "--date=iso",
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"  Git command failed: {exc}")
        return []

    commits: List[dict] = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 4)
        if len(parts) < 5:
            continue

        commit_hash, author, email, date_str, message = parts

        # Get changed files for this commit
        files = _get_commit_files(repo, commit_hash)

        commits.append({
            "id": f"commit_{commit_hash[:8]}",
            "hash": commit_hash,
            "author": author,
            "email": email,
            "date": date_str,
            "message": message,
            "files": files,
            "summary": f"{author} committed: {message} (files: {', '.join(files[:5])})",
        })

    return commits


def _get_commit_files(repo: Path, commit_hash: str) -> List[str]:
    """Get the list of files changed in a commit."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff-tree", "--no-commit-id",
             "--name-only", "-r", commit_hash],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        return []


def save_commits(commits: List[dict], output_path: str | Path) -> Path:
    """Save commit records as JSON (compatible with existing data format)."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(commits, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------------------
# Full ingestion pipeline
# ---------------------------------------------------------------------------

def ingest_all(
    repo_path: Optional[str | Path] = None,
    output_dir: str | Path = ROOT / "data" / "live",
    reindex: bool = True,
) -> Dict[str, int]:
    """Ingest all available real data sources.

    Returns a dict of source_name → record_count.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    counts: Dict[str, int] = {}

    # 1. Git history
    if repo_path:
        print(f"Ingesting git history from {repo_path}...")
        commits = ingest_git_history(repo_path)
        if commits:
            save_commits(commits, output / "git_commits.json")
            counts["git_commits"] = len(commits)
            print(f"  → {len(commits)} commits saved to {output / 'git_commits.json'}")

    # 2. Re-index the hybrid retriever if requested
    if reindex and counts:
        print("Re-indexing hybrid retriever with live data...")
        try:
            from src.retrieval import get_hybrid_retriever
            retriever = get_hybrid_retriever()
            n = retriever.index_all(reset=True)
            counts["retriever_docs"] = n
            print(f"  → {n} documents indexed")
        except Exception as exc:
            print(f"  → Indexing skipped: {exc}")

    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    repo = None
    if "--git" in sys.argv:
        idx = sys.argv.index("--git")
        if idx + 1 < len(sys.argv):
            repo = sys.argv[idx + 1]

    # Default: ingest from the current project
    if repo is None:
        repo = ROOT

    counts = ingest_all(repo_path=repo)
    print(f"\nDone. Ingested: {counts}")
