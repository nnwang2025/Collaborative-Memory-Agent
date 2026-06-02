"""
Conversation memory for multi-turn dialogue.

Maintains a sliding window of recent messages and provides context
for follow-up questions (e.g., "那第二个方案呢？" should know what
"第二个方案" refers to based on previous turns).
"""

from __future__ import annotations

from typing import Dict, List, Optional


class ConversationMemory:
    """Sliding-window conversation history with optional summarization.

    Parameters
    ----------
    max_turns:
        Maximum number of Q&A pairs to keep in the active window.
        Older turns are summarized to conserve context.
    """

    def __init__(self, max_turns: int = 10):
        self.max_turns = max_turns
        self._turns: List[Dict[str, str]] = []  # [{"q": ..., "a": ...}, ...]
        self._summary: str = ""

    def add(self, question: str, answer: str) -> None:
        """Record a completed Q&A turn."""
        self._turns.append({"q": question, "a": answer})
        # Summarize oldest turns if window overflows
        if len(self._turns) > self.max_turns:
            overflow = self._turns[:-self.max_turns]
            self._turns = self._turns[-self.max_turns:]
            self._summary = self._summarize_turns(overflow)

    def get_context(self, max_chars: int = 1500) -> str:
        """Build a context string for the next turn's routing/answering.

        Includes:
          - A summary of older turns (if any)
          - The last N Q&A pairs in the active window
        """
        parts: List[str] = []

        if self._summary:
            parts.append(f"[更早的对话摘要] {self._summary}")

        for i, turn in enumerate(self._turns):
            q_short = turn["q"][:200]
            a_short = turn["a"][:300]
            parts.append(f"Q{i+1}: {q_short}")
            parts.append(f"A{i+1}: {a_short}")

        ctx = "\n".join(parts)
        if len(ctx) > max_chars:
            ctx = ctx[:max_chars] + "..."
        return ctx

    def enrich_question(self, question: str) -> str:
        """Enrich a follow-up question with conversation context.

        If the question is a short follow-up (e.g., "为什么？", "第二个呢？"),
        prepend the previous Q&A for disambiguation.
        """
        q = question.strip()
        is_followup = (
            len(q) <= 6
            or any(q.startswith(w) for w in ["那", "这", "它", "为什么", "怎么", "什么", "第二个", "第一个", "还有", "其他"])
        )

        if is_followup and self._turns:
            prev = self._turns[-1]
            return f"[上文：用户问了「{prev['q'][:150]}」，系统回答了「{prev['a'][:150]}」]\n现在用户追问：{q}"
        return q

    @staticmethod
    def _summarize_turns(turns: List[Dict[str, str]]) -> str:
        """Create a brief summary of older conversation turns."""
        if not turns:
            return ""
        topics = [t["q"][:60] for t in turns]
        return f"讨论了{'；'.join(topics[:5])}等话题。"

    def clear(self) -> None:
        """Reset all conversation state."""
        self._turns.clear()
        self._summary = ""

    def __len__(self) -> int:
        return len(self._turns)

    def __bool__(self) -> bool:
        # Always True – the object is valid even when empty.
        # Use ``has_history()`` to check whether there's stored context.
        return True

    def has_history(self) -> bool:
        """True if there are recorded turns or a summary."""
        return len(self._turns) > 0 or bool(self._summary)
