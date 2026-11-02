"""
Coordinator Agent – routes user questions to skills and composes answers.

Supports two backends (selectable via ``use_langgraph``):
  * LangGraph backend (default) – full StateGraph with parallel skill
    execution, explicit state management, and a visualizable graph.
  * Linear backend – the original sequential pipeline, kept for A/B
    comparison and as a fallback.

Routing supports two strategies (auto-selected):
  * LLM few-shot classification (primary) – calls Qwen via SiliconFlow
    to classify the question intent.  More accurate for ambiguous or
    natural-language queries.
  * Keyword scoring (fallback) – original hardcoded signal dictionary.
    Always available, runs locally with zero latency.
"""

import json
import os
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from src.agents.answer_agent import AnswerAgent
from src.llm_config import resolve_api_key, resolve_base_url, resolve_model_name
from src.skills.registry import SkillRegistry


# ---------------------------------------------------------------------------
# LLM intent classification prompt
# ---------------------------------------------------------------------------

_INTENT_SYSTEM_PROMPT = """\
你是研发助手路由器。分析用户问题，输出应该调用哪个 Skill。

Skill 列表（只能从下列5个中选）：
- memory: 历史讨论/技术决策/长期记忆
- problem: Bug/Issue/日志排查/故障复盘
- dependency: 代码依赖/调用链/模块关系
- document: 架构文档/技术方案/设计说明
- entrypoint: 入口文件/启动流程/CLI

严格按以下格式输出，每行一个 Skill 加分数（0-10）：
SKILL: memory=分数
SKILL: problem=分数
SKILL: dependency=分数
SKILL: document=分数
SKILL: entrypoint=分数
TOP: 最高分skill名

分数: 8-10=最佳, 5-7=有用补充, 1-4=略微相关, 0=无关

示例1:
问题: 为什么采用混合检索而不是纯向量检索？
SKILL: memory=9
SKILL: problem=0
SKILL: dependency=0
SKILL: document=7
SKILL: entrypoint=0
TOP: memory

示例2:
问题: MemorySkill是怎么被调用的？
SKILL: memory=3
SKILL: problem=0
SKILL: dependency=9
SKILL: document=2
SKILL: entrypoint=0
TOP: dependency

示例3:
问题: 项目的入口在哪里？
SKILL: memory=0
SKILL: problem=0
SKILL: dependency=0
SKILL: document=2
SKILL: entrypoint=9
TOP: entrypoint

示例4:
问题: Rerank问题怎么修复？
SKILL: memory=5
SKILL: problem=8
SKILL: dependency=0
SKILL: document=4
SKILL: entrypoint=0
TOP: problem
"""


class CoordinatorAgent:
    """Score-based Coordinator Agent that routes questions to 1-2 skills.

    Parameters
    ----------
    registry:
        Skill registry.  Created automatically when not provided.
    answer_agent:
        Answer composing agent.  Created automatically when not provided.
    use_langgraph:
        When True (the default) ``ask()`` runs the question through the
        LangGraph StateGraph defined in ``src.graph``.  When False it
        falls back to the original sequential pipeline.
    use_llm_routing:
        When True (the default) route_scores() tries LLM classification
        first and falls back to keyword scoring on failure.
    """

    # ------------------------------------------------------------------
    # Signal dictionaries (unchanged – the heart of keyword-based routing)
    # ------------------------------------------------------------------

    ROUTE_SIGNALS: Dict[str, List[str]] = {
        "document": ["设计", "方案", "说明", "文档", "架构", "技术选型", "为什么采用", "接口", "readme"],
        "problem": ["bug", "报错", "异常", "traceback", "issue", "修复", "复盘", "故障", "timeout", "leak", "日志"],
        "entrypoint": ["入口", "启动", "main", "运行流程", "cli", "server", "app"],
        "dependency": ["依赖", "import", "调用关系", "模块关系", "类", "函数", "package", "调用链", "被谁调用"],
        "memory": ["之前", "上次", "讨论过", "历史", "决策", "原因", "背景", "为什么"],
    }

    DECISION_SIGNALS = ["为什么", "原因", "采用", "设计", "方案", "决策", "技术选型", "没有采用"]
    RETROSPECTIVE_SIGNALS = ["之前", "上次", "历史"]
    PROBLEM_STRONG_SIGNALS = ["bug", "报错", "修复", "issue", "log", "日志", "故障", "timeout", "leak"]
    ENTRYPOINT_STRONG_SIGNALS = ["入口", "main", "启动", "从哪里启动"]
    DEPENDENCY_STRONG_SIGNALS = ["依赖", "import", "调用", "函数", "类", "调用链", "被谁调用", "执行流程"]
    TECH_DOC_SIGNALS = [
        "agent", "rag", "bm25", "embedding", "rerank", "memory", "langgraph",
        "skill", "实体抽取", "混合检索", "长期记忆", "prompt", "架构", "接口", "技术选型", "方案", "文档",
    ]
    CHANGE_HISTORY_SIGNALS = ["谁修改", "修改过", "谁改", "改过", "commit", "提交"]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(
        self,
        registry: Optional[SkillRegistry] = None,
        answer_agent: Optional[AnswerAgent] = None,
        use_langgraph: bool = True,
        use_llm_routing: bool = True,
        enable_conversation_memory: bool = True,
        scope=None,
        db_path=None,
        live_dir=None,
    ):
        self.registry = registry or SkillRegistry(scope=scope, db_path=db_path, live_dir=live_dir)
        self.answer_agent = answer_agent or AnswerAgent()
        self.use_langgraph = use_langgraph
        self.use_llm_routing = use_llm_routing
        self.enable_conversation_memory = enable_conversation_memory
        self._graph = None  # lazily compiled LangGraph instance

        # Multi-turn conversation memory
        if enable_conversation_memory:
            from src.memory.conversation import ConversationMemory
            self.conversation = ConversationMemory(max_turns=10)
        else:
            self.conversation = None

    # ------------------------------------------------------------------
    # Routing (shared by both backends)
    # ------------------------------------------------------------------

    def route_scores(self, question: str) -> Dict[str, int]:
        """Per-skill relevance scoring.

        Tries LLM few-shot classification first (when
        ``use_llm_routing=True`` and ``SILICONFLOW_API_KEY`` is set).
        Falls back to keyword-based scoring on any failure.
        """
        if self.use_llm_routing:
            llm_scores = self._llm_route_scores(question)
            if llm_scores is not None:
                return llm_scores

        return self._keyword_route_scores(question)

    def _keyword_route_scores(self, question: str) -> Dict[str, int]:
        """Keyword-based per-skill relevance scoring (original logic)."""
        q = (question or "").lower()
        scores: Dict[str, int] = {skill: 0 for skill in self.ROUTE_SIGNALS}

        for skill, signals in self.ROUTE_SIGNALS.items():
            for signal in signals:
                if signal.lower() in q:
                    scores[skill] += 2

        if any(signal in q for signal in self.DECISION_SIGNALS):
            scores["document"] += 2
            scores["memory"] += 3
        if any(signal in q for signal in self.RETROSPECTIVE_SIGNALS):
            scores["memory"] += 3
        if any(signal in q for signal in self.PROBLEM_STRONG_SIGNALS):
            scores["problem"] += 6
        if any(signal in q for signal in self.ENTRYPOINT_STRONG_SIGNALS):
            scores["entrypoint"] += 4
        if any(signal in q for signal in self.DEPENDENCY_STRONG_SIGNALS):
            scores["dependency"] += 5

        tech_doc_hits = sum(1 for signal in self.TECH_DOC_SIGNALS if signal in q)
        if tech_doc_hits:
            scores["document"] += 2 * tech_doc_hits
        if tech_doc_hits and any(signal in q for signal in self.DECISION_SIGNALS):
            scores["document"] += 3
            scores["memory"] += 2
        if any(signal in q for signal in self.CHANGE_HISTORY_SIGNALS):
            scores["problem"] += 3
            scores["memory"] += 2

        return scores

    def route(self, question: str) -> List[str]:
        """Pick 1-2 skill names from the scores."""
        scores = self.route_scores(question)
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)

        if ranked[0][1] < 2:
            return ["memory", "document"]

        selected = [ranked[0][0]]
        if ranked[1][1] > 0 and ranked[0][1] - ranked[1][1] <= 1:
            selected.append(ranked[1][0])

        q = (question or "").lower()
        is_decision_or_history = any(
            signal in q for signal in self.DECISION_SIGNALS + self.RETROSPECTIVE_SIGNALS
        )
        if selected == ["document"] and scores.get("memory", 0) >= 5 and is_decision_or_history:
            selected.append("memory")

        return selected

    # ------------------------------------------------------------------
    # LLM-based intent classification
    # ------------------------------------------------------------------

    @staticmethod
    def _llm_route_scores(question: str) -> Optional[Dict[str, int]]:
        """Classify the question with the configured chat model.

        Returns a ``{skill: score}`` dict on success, ``None`` on failure
        (so the caller falls back to keyword scoring).
        """
        # DeepSeek official settings are canonical; legacy provider variables
        # remain supported.  Placeholder template keys count as unconfigured.
        base_url = resolve_base_url()
        api_key = resolve_api_key(base_url)
        if not api_key:
            return None

        model = resolve_model_name(base_url)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": f"问题：{question}"},
            ],
            "temperature": 0.0,
            "max_tokens": 256,
        }

        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                OSError):
            return None

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None

        # Parse JSON from LLM response (may be wrapped in ```json ... ```)
        return CoordinatorAgent._parse_llm_scores(content)

    @staticmethod
    def _parse_llm_scores(raw: str) -> Optional[Dict[str, int]]:
        """Parse LLM response into skill scores.

        Handles two formats:
          1. Line-based (preferred for small models):
             ``SKILL: memory=9``
          2. JSON (fallback): ``{"scores": {...}}``
        """
        text = raw.strip()
        scores: Dict[str, int] = {}

        # Format 1: SKILL: name=value lines
        import re
        for line in text.split("\n"):
            m = re.match(r"SKILL:\s*(\w+)\s*=\s*(\d+)", line.strip(), re.IGNORECASE)
            if m:
                skill = m.group(1).lower()
                if skill in ("memory", "problem", "dependency", "document", "entrypoint"):
                    scores[skill] = max(0, min(10, int(m.group(2))))

        if len(scores) >= 3:  # at least 3 valid skills parsed
            # Fill missing skills with 0
            for s in ("memory", "problem", "dependency", "document", "entrypoint"):
                scores.setdefault(s, 0)
            return scores

        # Format 2: JSON (try if line parsing didn't yield enough skills)
        # Strip optional markdown fences
        json_text = text
        if json_text.startswith("```"):
            lines = json_text.split("\n")
            if len(lines) >= 3:
                json_text = "\n".join(lines[1:-1]).strip()

        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            # Try to find a JSON object
            match = re.search(r"\{[^{}]*\"scores\"[^{}]*\}", json_text, re.DOTALL)
            if not match:
                return scores if len(scores) >= 3 else None
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return scores if len(scores) >= 3 else None

        raw_scores = parsed.get("scores", {})
        for skill in ("memory", "problem", "dependency", "document", "entrypoint"):
            val = float(raw_scores.get(skill, 0))
            scores[skill] = max(0, min(10, int(round(val))))

        if all(v == 0 for v in scores.values()):
            return None
        return scores

    # ------------------------------------------------------------------
    # ask() – the main entry point
    # ------------------------------------------------------------------

    def ask(self, question: str) -> str:
        """Answer a user question with multi-turn context.

        Delegates to either the LangGraph workflow or the original linear
        pipeline depending on ``self.use_langgraph``.
        """
        # Enrich follow-up questions with conversation context
        enriched = question
        if self.conversation is not None:
            enriched = self.conversation.enrich_question(question)

        if self.use_langgraph:
            answer = self._ask_with_graph(enriched)
        else:
            answer = self._ask_linear(enriched)

        # Record the turn for future context
        if self.conversation is not None:
            self.conversation.add(question, answer[:500])

        return answer

    # ------------------------------------------------------------------
    # LangGraph backend
    # ------------------------------------------------------------------

    def _ask_with_graph(self, question: str) -> str:
        """Run the question through the LangGraph StateGraph."""
        # Pre-warm the hybrid retriever (model + FAISS index) in the main
        # thread BEFORE LangGraph fans out to parallel skill nodes.  This
        # avoids a race condition where two nodes try to load
        # sentence_transformers / faiss simultaneously.
        if self.registry.scope.dataset_kind == "fixture":
            self._warm_hybrid_retriever()

        from src.graph import get_graph

        graph = get_graph()

        initial_state = {
            "scope": self.registry.scope.model_dump(),
            "db_path": str(self.registry.db_path) if self.registry.db_path is not None else None,
            "live_dir": str(self.registry.live_dir) if self.registry.live_dir is not None else None,
            "use_llm_routing": self.use_llm_routing,
            "question": question,
            "scores": {},
            "skill_names": [],
            "skill_results": {},
            "ranked_evidence": [],
            "answer": "",
        }

        result = graph.invoke(initial_state)
        return result["answer"]

    @staticmethod
    def _warm_hybrid_retriever() -> None:
        """Ensure the hybrid retriever is loaded before parallel execution."""
        try:
            from src.retrieval import get_hybrid_retriever
            retriever = get_hybrid_retriever()
            retriever._ensure_loaded()
        except Exception:
            pass  # hybrid retrieval is optional; skills fall back to BM25

    async def astream(self, question: str):
        """Async-generate tokens via LangGraph's astream.

        Yields per-node updates so callers can show progress (e.g.
        "classifying intent…", "retrieving from memory…").
        """
        from src.graph import get_graph

        graph = get_graph()

        initial_state = {
            "scope": self.registry.scope.model_dump(),
            "db_path": str(self.registry.db_path) if self.registry.db_path is not None else None,
            "live_dir": str(self.registry.live_dir) if self.registry.live_dir is not None else None,
            "use_llm_routing": self.use_llm_routing,
            "question": question,
            "scores": {},
            "skill_names": [],
            "skill_results": {},
            "ranked_evidence": [],
            "answer": "",
        }

        async for chunk in graph.astream(initial_state):
            yield chunk

    # ------------------------------------------------------------------
    # Linear backend (original sequential pipeline)
    # ------------------------------------------------------------------

    def _ask_linear(self, question: str) -> str:
        """Original sequential pipeline, kept for comparison."""
        scores = self.route_scores(question)
        skill_names = self.route(question)

        results: Dict[str, dict] = {}
        for name in skill_names:
            skill = self.registry.get(name)
            results[name] = skill.run(question)

        return self.answer_agent.compose(question, skill_names, results, scores)
