"""
LangGraph workflow for Collaborative Memory Agent.

Replaces the linear CoordinatorAgent.ask() pipeline with a proper StateGraph:
  classify_intent → route_skills → [fan-out to selected skills in parallel]
                                      ├── memory_retrieve
                                      ├── problem_retrieve
                                      ├── dependency_retrieve
                                      ├── document_retrieve
                                      └── entrypoint_retrieve
                                   [fan-in]
                                        ↓
                                   merge_rerank → generate_answer → END

Key benefits over the linear pipeline:
  - Parallel skill execution (LangGraph handles fan-out/fan-in)
  - Explicit state management with typed state
  - Observable graph structure for debugging and visualization
  - Extensible: add new nodes (reflection, verification, etc.) without
    touching existing code
"""

from typing import TypedDict, Dict, List, Any, Sequence, Annotated

from langgraph.graph import StateGraph, END

from src.agents.answer_agent import AnswerAgent
from src.skills.registry import SkillRegistry


# ---------------------------------------------------------------------------
# Reducers (for merging parallel-branch updates)
# ---------------------------------------------------------------------------

def _merge_dicts(left: dict, right: dict) -> dict:
    """Reducer: shallow-merge *right* into *left*.

    Used by LangGraph when multiple parallel branches write to
    ``skill_results``.  Because each branch writes a different key
    there are no real conflicts; this reducer just accumulates them.
    """
    merged = dict(left)
    merged.update(right)
    return merged


# ---------------------------------------------------------------------------
# Typed State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    """Fully-typed state that flows through the LangGraph.

    Every node reads from and writes to this state dict.  Keys annotated
    with ``Annotated[..., reducer]`` tell LangGraph how to merge updates
    from parallel (fan-out) branches.
    """

    question: str
    """Raw user question."""

    scores: Dict[str, int]
    """Per-skill relevance scores from classify_intent (keyword + LLM)."""

    skill_names: List[str]
    """Names of the 1-2 skills selected by route_skills."""

    skill_results: Annotated[Dict[str, Any], _merge_dicts]
    """Accumulated results from parallel skill nodes.  Each branch writes
    its own key (e.g. ``{"memory": {...}}``); the reducer merges them."""

    ranked_evidence: List[Dict]
    """Merged, deduplicated and re-ranked evidence items ready for the LLM."""

    answer: str
    """Final structured answer (the markdown output returned to the user)."""


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

def classify_intent_node(state: AgentState) -> dict:
    """Classify the user question and assign per-skill relevance scores.

    Currently delegates to CoordinatorAgent.route_scores() (keyword-based).
    When P0-3 lands this will call the LLM intent classifier instead, but
    the node boundary stays identical – no downstream nodes change.
    """
    # Re-use the keyword-based scorer for now; P0-3 replaces this with LLM.
    from src.agents.coordinator_agent import CoordinatorAgent

    coordinator = CoordinatorAgent()
    scores = coordinator.route_scores(state["question"])
    return {"scores": scores}


def route_skills_node(state: AgentState) -> dict:
    """Select 1-2 skills based on classify_intent scores.

    Rules (kept identical to the current CoordinatorAgent.route()):
      - If top score < 2, default to ["memory", "document"].
      - Always pick the top-scoring skill.
      - If 2nd place is within 1 point, pick it too.
      - Special case: if only "document" was picked but decision/history
        signals give memory >= 5, add memory as well.
    """
    from src.agents.coordinator_agent import CoordinatorAgent

    coordinator = CoordinatorAgent()
    skill_names = coordinator.route(state["question"])
    return {"skill_names": skill_names}


# ---------------------------------------------------------------------------
# Per-skill retrieval nodes (run in parallel via LangGraph fan-out)
# ---------------------------------------------------------------------------

def _make_skill_node(skill_name: str):
    """Factory that returns a node function for *one* skill.

    Each node returns only its own contribution (e.g.
    ``{"skill_results": {"memory": {...}}}``).  LangGraph's reducer
    (``_merge_dicts``) handles merging across parallel branches so
    the final ``skill_results`` contains all selected skills.

    This design means each parallel branch is *independent* – it doesn't
    read or depend on other branches' state, which is the ideal pattern
    for LangGraph fan-out.
    """

    def skill_node(state: AgentState) -> dict:
        registry = SkillRegistry()
        skill = registry.get(skill_name)
        result = skill.run(state["question"])
        # Return only this skill's contribution; the reducer merges.
        return {"skill_results": {skill_name: result}}

    # Give each function a readable name for LangGraph's graph visualization.
    skill_node.__name__ = f"{skill_name}_retrieve"
    return skill_node


memory_retrieve_node = _make_skill_node("memory")
problem_retrieve_node = _make_skill_node("problem")
dependency_retrieve_node = _make_skill_node("dependency")
document_retrieve_node = _make_skill_node("document")
entrypoint_retrieve_node = _make_skill_node("entrypoint")


# ---------------------------------------------------------------------------
# Fan-out routing function
# ---------------------------------------------------------------------------

def _select_skill_nodes(state: AgentState) -> Sequence[str]:
    """Return the list of skill node names to execute in parallel.

    LangGraph calls this after route_skills exits.  Returning a *list*
    triggers fan-out: all named nodes start executing concurrently and
    their results are merged when they all reach the next common node.

    If no skills were selected (shouldn't happen with the current route
    logic) we skip straight to merge_rerank.
    """
    names = state.get("skill_names", [])
    if not names:
        return ["merge_rerank"]  # skip retrieval entirely
    return [f"{n}_retrieve" for n in names]


# ---------------------------------------------------------------------------
# Merge & Generate
# ---------------------------------------------------------------------------

def merge_rerank_node(state: AgentState) -> dict:
    """Collect evidence from all skill results, deduplicate, and rank.

    Delegates the heavy lifting (top_relevant scoring, trimming to top-8)
    to AnswerAgent so the ranking logic stays in one place.
    """
    answer_agent = AnswerAgent()
    skill_names = state.get("skill_names", [])
    skill_results = state.get("skill_results", {})

    # Flatten evidence from all selected skills.
    evidence: List[Dict] = []
    for name in skill_names:
        result = skill_results.get(name, {})
        for item in result.get("evidence", []):
            copied = dict(item)
            copied["skill"] = name
            evidence.append(copied)

    # Rank and keep top-8 (same as current compose logic).
    ranked = answer_agent._top_relevant(state["question"], evidence)[:8]
    return {"ranked_evidence": ranked}


def generate_answer_node(state: AgentState) -> dict:
    """Compose the final structured answer from ranked evidence.

    Uses AnswerAgent.compose() so the output format stays identical to
    the current linear pipeline: 【回答】, 【关键证据】, 【来源文件】,
    【关联实体】, 【置信度】, 【路由结果】.
    """
    answer_agent = AnswerAgent()
    skill_names = state.get("skill_names", [])
    skill_results = state.get("skill_results", {})
    scores = state.get("scores", {})

    answer = answer_agent.compose(
        question=state["question"],
        skill_names=skill_names,
        skill_results=skill_results,
        route_scores=scores,
    )
    return {"answer": answer}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

# Map skill-name → node-name for the conditional edge mapping.
_SKILL_NODE_MAP = {
    "memory_retrieve": "memory_retrieve",
    "problem_retrieve": "problem_retrieve",
    "dependency_retrieve": "dependency_retrieve",
    "document_retrieve": "document_retrieve",
    "entrypoint_retrieve": "entrypoint_retrieve",
    "merge_rerank": "merge_rerank",  # fallback when no skills selected
}


def build_graph() -> StateGraph:
    """Build and compile the Collaborative Memory Agent LangGraph.

    Returns a compiled graph ready to .invoke() or .astream().
    """
    graph = StateGraph(AgentState)

    # -- Add nodes ----------------------------------------------------------
    graph.add_node("classify_intent", classify_intent_node)
    graph.add_node("route_skills", route_skills_node)
    graph.add_node("memory_retrieve", memory_retrieve_node)
    graph.add_node("problem_retrieve", problem_retrieve_node)
    graph.add_node("dependency_retrieve", dependency_retrieve_node)
    graph.add_node("document_retrieve", document_retrieve_node)
    graph.add_node("entrypoint_retrieve", entrypoint_retrieve_node)
    graph.add_node("merge_rerank", merge_rerank_node)
    graph.add_node("generate_answer", generate_answer_node)

    # -- Wire edges ---------------------------------------------------------
    graph.set_entry_point("classify_intent")
    graph.add_edge("classify_intent", "route_skills")

    # Fan-out: route_skills → [selected skill nodes in parallel]
    graph.add_conditional_edges(
        "route_skills",
        _select_skill_nodes,
        _SKILL_NODE_MAP,
    )

    # Fan-in: every skill node → merge_rerank
    for node_name in [
        "memory_retrieve",
        "problem_retrieve",
        "dependency_retrieve",
        "document_retrieve",
        "entrypoint_retrieve",
    ]:
        graph.add_edge(node_name, "merge_rerank")

    graph.add_edge("merge_rerank", "generate_answer")
    graph.add_edge("generate_answer", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Singleton convenience
# ---------------------------------------------------------------------------

_graph_instance: StateGraph | None = None


def get_graph() -> StateGraph:
    """Return a module-level compiled graph instance (lazy init)."""
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = build_graph()
    return _graph_instance
