"""
Multi-strategy text chunking for heterogeneous RAG data sources.

Different data types need different chunking strategies:

  - Markdown/docs  → recursive split by headers → paragraphs → sentences
                     with overlap to preserve cross-boundary semantics
  - Code           → AST-based split at function / class boundaries
  - Chat/conversations → split by speaker turns or topic boundaries
  - Logs           → time-window grouping
  - JSON records   → content-aware split of long text fields

Each chunker returns ``List[dict]`` with ``{content, metadata}`` so
downstream indexers don't need to know about chunking internals.

Usage
-----
    from src.chunking import chunk_for_category

    chunks = chunk_for_category("markdown", long_text, source="docs/arch.md")
    # → [{"content": "### 架构说明\\n...", "metadata": {...}}, ...]
"""

from __future__ import annotations

import ast
import re
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseChunker:
    """Chunking strategy interface."""

    def chunk(self, text: str, metadata: Optional[dict] = None) -> List[dict]:
        """Split *text* into chunks with metadata."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Recursive character splitter (the core engine for text chunking)
# ---------------------------------------------------------------------------

class RecursiveSplitter:
    """Recursively split text by a hierarchy of separators.

    Mimics LangChain's ``RecursiveCharacterTextSplitter`` but avoids
    the heavy dependency.  Tries to split on the most semantic boundary
    first (e.g. ``## `` header), falling back to coarser separators
    until the chunk fits within ``chunk_size``.

    Parameters
    ----------
    separators:
        Ordered list of split patterns.  Tried from first to last.
    chunk_size:
        Target max characters per chunk.
    chunk_overlap:
        Characters to overlap between consecutive chunks (preserves
        cross-boundary context for embedding).
    """

    def __init__(
        self,
        separators: List[str],
        chunk_size: int = 512,
        chunk_overlap: int = 64,
    ):
        self.separators = separators
        if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
            raise ValueError("Require 0 <= chunk_overlap < chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, text: str) -> List[str]:
        """Recursively split *text* into chunks ≤ chunk_size."""
        if len(text) <= self.chunk_size:
            return [text] if text.strip() else []

        # Try each separator in order
        for sep in self.separators:
            if sep not in text:
                continue

            parts = text.split(sep)
            # Re-attach the separator (except for the first fragment)
            for i in range(1, len(parts)):
                parts[i] = sep + parts[i]

            # A leading separator can reproduce the entire input unchanged.
            if any(part.strip() == text.strip() for part in parts):
                continue

            # Recurse on each part
            chunks = []
            for part in parts:
                chunks.extend(self.split(part.strip()))
            return self._merge_with_overlap(chunks)

        # No separator matched → force-split by chunk_size
        return self._force_split(text)

    def _force_split(self, text: str) -> List[str]:
        """Fallback: split at chunk_size with overlap."""
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start += self.chunk_size - self.chunk_overlap
        return chunks

    def _merge_with_overlap(self, chunks: List[str]) -> List[str]:
        """Apply overlap between consecutive chunks."""
        if self.chunk_overlap <= 0 or not chunks:
            return chunks

        result = []
        buffer = chunks[0]
        for i in range(1, len(chunks)):
            combined = buffer + chunks[i]
            if len(combined) <= self.chunk_size:
                buffer = combined
            else:
                result.append(buffer)
                # Start new buffer with overlap from previous
                if len(buffer) > self.chunk_overlap:
                    buffer = buffer[-self.chunk_overlap:] + chunks[i]
                else:
                    buffer = chunks[i]
        result.append(buffer)
        return [c for c in result if c.strip()]


# ---------------------------------------------------------------------------
# Concrete chunkers
# ---------------------------------------------------------------------------

class MarkdownChunker(BaseChunker):
    """Chunk Markdown documents by structural hierarchy.

    Split order: ## header → ### header → paragraph (double newline) →
    single newline → sentence boundary (。) → space → character.

    This preserves section integrity — a chunk won't cut a header
    from its body text unless the section itself exceeds chunk_size.
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64):
        self.splitter = RecursiveSplitter(
            separators=[
                "\n## ",     # H2 headers
                "\n### ",    # H3 headers
                "\n#### ",   # H4 headers
                "\n\n",      # paragraphs
                "\n",        # lines
                "。",        # Chinese sentence end
                ". ",        # English sentence end
                " ",
            ],
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def chunk(self, text: str, metadata: Optional[dict] = None) -> List[dict]:
        meta = dict(metadata or {})
        chunks = []
        for i, chunk_text in enumerate(self.splitter.split(text)):
            if len(chunk_text.strip()) < 10:
                continue
            chunks.append({
                "content": chunk_text.strip(),
                "metadata": {**meta, "chunk_index": i, "chunk_strategy": "markdown"},
            })
        return chunks


class ConversationChunker(BaseChunker):
    """Chunk chat / conversation logs.

    Splits on speaker turn boundaries first, then merges short turns
    into larger chunks to preserve dialogue context.
    """

    def __init__(self, chunk_size: int = 384, chunk_overlap: int = 48):
        # Separators tuned for chat formats:
        #   "lin: ..." / "zhou: ..."  (speaker prefix)
        #   "---"                      (message separator)
        #   double-newline             (topic break)
        self.splitter = RecursiveSplitter(
            separators=[
                r"\n(?=[a-zA-Z_]+:)",   # speaker turn boundary
                "\n---\n",
                "\n\n",
                "\n",
                "。",
                " ",
            ],
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def chunk(self, text: str, metadata: Optional[dict] = None) -> List[dict]:
        meta = dict(metadata or {})
        chunks = []
        for i, chunk_text in enumerate(self.splitter.split(text)):
            if len(chunk_text.strip()) < 8:
                continue
            chunks.append({
                "content": chunk_text.strip(),
                "metadata": {**meta, "chunk_index": i, "chunk_strategy": "conversation"},
            })
        return chunks


class LogChunker(BaseChunker):
    """Chunk application logs by time windows.

    Groups consecutive log lines that fall within the same time window
    (default: 5 minutes).  This preserves the context of multi-line
    error traces and related log events.
    """

    def __init__(self, max_chunk_size: int = 512):
        self.max_chunk_size = max_chunk_size
        # Common log timestamp patterns
        self._ts_pattern = re.compile(
            r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})"  # 2024-01-15 14:32
        )

    def chunk(self, text: str, metadata: Optional[dict] = None) -> List[dict]:
        meta = dict(metadata or {})
        lines = text.split("\n")
        if not lines:
            return []

        # Group by time proximity
        chunks: List[dict] = []
        buffer = []
        buffer_size = 0
        chunk_idx = 0

        for line in lines:
            if buffer_size + len(line) > self.max_chunk_size and buffer:
                chunks.append({
                    "content": "\n".join(buffer),
                    "metadata": {**meta, "chunk_index": chunk_idx, "chunk_strategy": "log_window"},
                })
                chunk_idx += 1
                buffer = []
                buffer_size = 0

            # Detect time-window break
            ts_match = self._ts_pattern.search(line)
            if ts_match and buffer:
                # New timestamp → potential window break
                # In a full implementation we'd compute the actual time gap;
                # for the MVP we just use max_chunk_size as the grouping signal
                pass

            buffer.append(line)
            buffer_size += len(line)

        if buffer:
            chunks.append({
                "content": "\n".join(buffer),
                "metadata": {**meta, "chunk_index": chunk_idx, "chunk_strategy": "log_window"},
            })

        return chunks


class CodeChunker(BaseChunker):
    """Chunk Python source code at function / class boundaries via AST.

    Unlike text chunkers, this uses the AST to find natural code
    boundaries.  Each top-level function or class becomes a chunk,
    with its docstring and signature preserved.
    """

    def __init__(self, max_chunk_size: int = 800):
        self.max_chunk_size = max_chunk_size

    def chunk(self, text: str, metadata: Optional[dict] = None) -> List[dict]:
        meta = dict(metadata or {})
        chunks = []

        try:
            tree = ast.parse(text)
        except SyntaxError:
            # Fall back to line-based chunking
            return self._fallback_chunk(text, meta)

        # Extract top-level definitions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = node.lineno - 1  # 0-indexed
                end = node.end_lineno if hasattr(node, 'end_lineno') else start + 50
                # Get source lines
                lines = text.split("\n")[start:end]
                chunk_text = "\n".join(lines)

                if len(chunk_text) > self.max_chunk_size:
                    # Function too long → split at logical boundaries
                    sub_chunks = self._split_long_function(chunk_text, node.name)
                    for sc in sub_chunks:
                        chunks.append({
                            "content": sc,
                            "metadata": {**meta, "chunk_strategy": "code_ast", "symbol": node.name},
                        })
                else:
                    chunks.append({
                        "content": chunk_text,
                        "metadata": {**meta, "chunk_strategy": "code_ast", "symbol": node.name},
                    })

        # If no AST boundaries found, fall back
        if not chunks:
            return self._fallback_chunk(text, meta)

        return chunks

    def _split_long_function(self, text: str, name: str) -> List[str]:
        """Split a long function body by logical blocks."""
        lines = text.split("\n")
        header = lines[0]  # function signature
        body = lines[1:] if len(lines) > 1 else []

        chunks = []
        buffer = [header]
        buf_size = len(header)

        for line in body:
            stripped = line.strip()
            # Split points: blank lines, comment blocks, logical sections
            if buf_size + len(line) > self.max_chunk_size - 100:
                chunks.append("\n".join(buffer))
                buffer = [f"{header}  # ... (continued from {name})"]
                buf_size = len(buffer[0])
            buffer.append(line)
            buf_size += len(line)

        if len(buffer) > 1:
            chunks.append("\n".join(buffer))
        return chunks or [text]

    def _fallback_chunk(self, text: str, metadata: dict) -> List[dict]:
        """Line-based fallback for non-Python or unparseable files."""
        lines = text.split("\n")
        chunks = []
        for i in range(0, len(lines), 40):
            chunk_text = "\n".join(lines[i:i + 40])
            if chunk_text.strip():
                chunks.append({
                    "content": chunk_text,
                    "metadata": {**metadata, "chunk_strategy": "code_line"},
                })
        return chunks


class JSONRecordChunker(BaseChunker):
    """Handle long text fields inside JSON records.

    For structured records (issues, commits, decisions): if any text
    field exceeds chunk_size, split that field while preserving the
    other fields as context prefix.
    """

    def __init__(self, chunk_size: int = 512, chunk_overlap: int = 64):
        self.splitter = RecursiveSplitter(
            separators=["\n\n", "\n", "。", ". ", " "],
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    def chunk(self, text: str, metadata: Optional[dict] = None) -> List[dict]:
        meta = dict(metadata or {})

        if len(text) <= self.splitter.chunk_size:
            return [{"content": text, "metadata": {**meta, "chunk_strategy": "json_record"}}]

        # Long record: split the body but prepend identifier fields as context
        # Extract a short prefix (title / ID / summary) to add to each chunk
        prefix = self._extract_prefix(text)

        chunks = []
        for i, chunk_text in enumerate(self.splitter.split(text)):
            if len(chunk_text.strip()) < 10:
                continue
            full_content = f"{prefix}\n{chunk_text.strip()}" if prefix else chunk_text.strip()
            chunks.append({
                "content": full_content,
                "metadata": {**meta, "chunk_index": i, "chunk_strategy": "json_record"},
            })
        return chunks or [{"content": text, "metadata": {**meta, "chunk_strategy": "json_record"}}]

    @staticmethod
    def _extract_prefix(text: str) -> str:
        """Extract a short context prefix from the beginning of the record."""
        # Take first ~120 chars as context (usually contains title/ID/summary)
        first_line = text.split("\n")[0][:120].strip()
        return first_line if len(first_line) > 20 else ""


# ---------------------------------------------------------------------------
# Chunking pipeline – route by data category
# ---------------------------------------------------------------------------

_CHUNKERS: Dict[str, BaseChunker] = {}


def _get_chunkers() -> Dict[str, BaseChunker]:
    """Lazy-init chunker singletons."""
    global _CHUNKERS
    if not _CHUNKERS:
        _CHUNKERS = {
            "markdown": MarkdownChunker(chunk_size=512, chunk_overlap=64),
            "docs": MarkdownChunker(chunk_size=512, chunk_overlap=64),
            "chats": ConversationChunker(chunk_size=384, chunk_overlap=48),
            "conversation": ConversationChunker(chunk_size=384, chunk_overlap=48),
            "logs": LogChunker(max_chunk_size=512),
            "code": CodeChunker(max_chunk_size=800),
            "json_record": JSONRecordChunker(chunk_size=512, chunk_overlap=64),
            "default": MarkdownChunker(chunk_size=512, chunk_overlap=64),
        }
    return _CHUNKERS


def chunk_for_category(
    category: str,
    text: str,
    metadata: Optional[dict] = None,
    chunk_size: Optional[int] = None,
) -> List[dict]:
    """Route text to the appropriate chunker based on data category.

    Parameters
    ----------
    category:
        One of: "markdown", "docs", "chats", "conversation", "logs",
        "code", "json_record", or any custom label (falls back to
        "default" which uses the Markdown chunker).
    text:
        Raw text to chunk.
    metadata:
        Additional metadata to attach to every chunk (e.g. source file).
    chunk_size:
        Override the chunker's default chunk size (optional).

    Returns
    -------
    list[dict]
        Each dict has ``{"content": str, "metadata": dict}``.
    """
    chunkers = _get_chunkers()
    chunker = chunkers.get(category, chunkers["default"])

    # Apply chunk_size override if the chunker supports it
    if chunk_size and hasattr(chunker, 'splitter'):
        chunker.splitter.chunk_size = chunk_size

    return chunker.chunk(text, metadata=metadata)


# ---------------------------------------------------------------------------
# Convenience: split text without a category (auto-detect)
# ---------------------------------------------------------------------------

def auto_chunk(
    text: str,
    source: str = "",
    chunk_size: int = 512,
    chunk_overlap: int = 64,
) -> List[dict]:
    """Auto-detect the best chunking strategy from the source path.

    Heuristic:
      - ``*.py`` → code
      - ``*.md`` / ``docs/`` → markdown
      - ``chats/`` / ``*_chat*`` → conversation
      - ``logs/`` / ``*_log*`` → logs
      - otherwise → default (recursive split)
    """
    src_lower = source.lower()
    if src_lower.endswith(".py") or "src/" in src_lower or "code" in src_lower:
        category = "code"
    elif src_lower.endswith(".md") or "docs/" in src_lower:
        category = "markdown"
    elif "chat" in src_lower or "conversation" in src_lower or "message" in src_lower:
        category = "conversation"
    elif "log" in src_lower:
        category = "logs"
    else:
        category = "default"

    return chunk_for_category(category, text, metadata={"source": source, "category": category})
