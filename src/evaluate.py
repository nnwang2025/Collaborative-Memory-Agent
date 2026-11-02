"""
Legacy demonstration diagnostics for the Collaborative Memory Agent.

These keyword-derived labels are not an independent quality benchmark.
Use python -m benchmark.runner for frozen evidence-labelled offline comparisons.

Measures:
  1. Retrieval quality: Hit@5 for pure-BM25 vs Hybrid (vector+BM25+entity)
  2. Routing accuracy: Keyword vs LLM intent classification
  3. Routing/evidence heuristics (not semantic answer correctness)

Usage:
    python -m src.evaluate
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Test set: (question, expected_skill, relevant_keywords_in_answer)
# ---------------------------------------------------------------------------

TEST_SET: List[Tuple[str, str, List[str]]] = [
    # Decision / memory queries
    ("为什么采用混合检索而不是纯向量检索？", "memory",
     ["混合检索", "BM25", "Embedding", "Rerank", "实体"]),
    ("历史上做过哪些技术决策？", "memory",
     ["决策", "架构", "混合检索", "长期记忆", "分层"]),
    ("之前讨论过哪些架构方案？", "memory",
     ["架构", "方案", "MVP", "Coordinator", "Skill"]),
    ("这个项目的长期记忆是怎么设计的？", "memory",
     ["长期记忆", "Semantic", "Episodic", "Entity", "Decision"]),

    # Problem / issue queries
    ("Rerank相关问题怎么修复？", "problem",
     ["Rerank", "修复", "权重", "证据", "重排"]),
    ("CMA-105这个issue是什么问题？", "problem",
     ["CMA-105", "Rerank", "优化", "检索"]),
    ("最近有哪些bug修复记录？", "problem",
     ["bug", "修复", "commit", "issue"]),

    # Dependency / call chain queries
    ("MemorySkill是怎么被调用的？", "dependency",
     ["MemorySkill", "CoordinatorAgent", "SkillRegistry", "调用", "run"]),
    ("CoordinatorAgent的ask方法执行流程是什么？", "dependency",
     ["CoordinatorAgent", "ask", "route", "compose"]),
    ("SkillRegistry里面有哪些Skill？", "dependency",
     ["SkillRegistry", "MemorySkill", "ProblemSkill", "DocumentSkill"]),

    # Document / architecture queries
    ("这个项目的架构是什么？", "document",
     ["架构", "Coordinator", "Skill", "Agent", "RAG"]),
    ("检索层是怎么设计的？", "document",
     ["检索", "混合检索", "BM25", "Embedding", "Rerank"]),
    ("Answer Agent的输出格式是什么？", "document",
     ["Answer", "回答", "证据", "来源", "置信度"]),

    # Entrypoint queries
    ("这个项目的入口在哪里？", "entrypoint",
     ["main.py", "入口", "CoordinatorAgent"]),
    ("怎么运行这个项目？", "entrypoint",
     ["main.py", "python", "CoordinatorAgent", "运行"]),

    # Mixed / edge cases
    ("为什么选择LangGraph而不是LangChain？", "memory",
     ["LangGraph", "LangChain", "StateGraph", "workflow"]),
    ("Entity Memory存储了什么？", "memory",
     ["Entity", "实体", "Agent", "Skill", "模块"]),
    ("Decision Memory和其他Memory有什么区别？", "memory",
     ["Decision", "决策", "Semantic", "Episodic", "区别"]),
    ("怎么添加一个新的Skill？", "document",
     ["Skill", "registry", "添加", "接口"]),
    ("项目里有哪些Agent类？", "dependency",
     ["Agent", "CoordinatorAgent", "AnswerAgent", "ReActAgent"]),
]


# ---------------------------------------------------------------------------
# Retrieval evaluation
# ---------------------------------------------------------------------------

def evaluate_retrieval() -> dict:
    """Compare BM25-only vs Hybrid retrieval using Hit@5."""
    from src.retrieval import HybridRetriever, _bm25_rank

    retriever = HybridRetriever()
    retriever._ensure_loaded()
    docs = retriever.docs

    bm25_hits = 0
    hybrid_hits = 0
    total_relevant = 0

    for question, expected_skill, keywords in TEST_SET:
        # Determine relevant docs: any doc containing >= 2 keywords
        relevant_ids = set()
        for doc in docs:
            content = doc.get("content", "").lower()
            matches = sum(1 for kw in keywords if kw.lower() in content)
            if matches >= 2:
                relevant_ids.add(doc["id"])
        if not relevant_ids:
            continue
        total_relevant += 1

        # BM25-only
        bm25_results = _bm25_rank(question, docs, top_k=5)
        bm25_ids = {r["id"] for r in bm25_results}
        if bm25_ids & relevant_ids:
            bm25_hits += 1

        # Hybrid
        hybrid_results = retriever.retrieve(
            question, top_k=5,
            dense_on=True, sparse_on=True, entity_on=True,
        )
        hybrid_ids = {r["id"] for r in hybrid_results}
        if hybrid_ids & relevant_ids:
            hybrid_hits += 1

    return {
        "total_queries": total_relevant,
        "skipped_queries": len(TEST_SET) - total_relevant,
        "label_method": "heuristic_keyword_overlap_not_independent_gold",
        "bm25_hit@5": round(bm25_hits / max(total_relevant, 1), 3),
        "hybrid_hit@5": round(hybrid_hits / max(total_relevant, 1), 3),
    }


# ---------------------------------------------------------------------------
# Routing evaluation
# ---------------------------------------------------------------------------

def evaluate_routing() -> dict:
    """Compare keyword vs LLM routing accuracy."""
    from src.agents.coordinator_agent import CoordinatorAgent

    agent_kw = CoordinatorAgent(use_llm_routing=False)
    agent_llm = CoordinatorAgent(use_llm_routing=True)

    kw_correct = 0
    llm_correct = 0
    total = 0

    for question, expected_skill, _ in TEST_SET:
        total += 1

        # Keyword routing
        kw_skills = agent_kw.route(question)
        if expected_skill in kw_skills:
            kw_correct += 1

        # LLM routing (falls back to keyword on failure)
        llm_skills = agent_llm.route(question)
        if expected_skill in llm_skills:
            llm_correct += 1

    return {
        "total_queries": total,
        "keyword_accuracy": round(kw_correct / max(total, 1), 3),
        "llm_or_keyword_fallback_accuracy": round(llm_correct / max(total, 1), 3),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Collaborative Memory Agent – Evaluation Benchmarks")
    print("=" * 60)

    # 1. Retrieval ablation
    print("\n 1. Retrieval Quality (Hit@5)")
    print("-" * 40)
    try:
        ret_results = evaluate_retrieval()
        print(f"  Test queries:         {ret_results['total_queries']}")
        print(f"  BM25-only Hit@5:   {ret_results['bm25_hit@5']:.1%}")
        print(f"  Hybrid Hit@5:      {ret_results['hybrid_hit@5']:.1%}")
        delta = ret_results['hybrid_hit@5'] - ret_results['bm25_hit@5']
        print(f"  Δ (Hybrid - BM25):    {delta:+.1%}")
    except Exception as exc:
        print(f"  Skipped: {exc}")

    # 2. Routing ablation
    print("\n 2. Routing Accuracy")
    print("-" * 40)
    try:
        route_results = evaluate_routing()
        print(f"  Test queries:         {route_results['total_queries']}")
        print(f"  Keyword routing:      {route_results['keyword_accuracy']:.1%}")
        print(f"  LLM routing:          {route_results['llm_or_keyword_fallback_accuracy']:.1%}")
    except Exception as exc:
        print(f"  Skipped: {exc}")

    # 3. Latency benchmark
    print("\n 3. Latency (single query)")
    print("-" * 40)
    try:
        from src.agents.coordinator_agent import CoordinatorAgent
        agent = CoordinatorAgent(use_langgraph=True, use_llm_routing=False)

        test_q = "为什么采用混合检索？"
        start = time.perf_counter()
        agent.ask(test_q)
        elapsed = time.perf_counter() - start
        print(f"  Query: {test_q}")
        print(f"  Time:  {elapsed:.2f}s (LangGraph + Keyword routing)")
    except Exception as exc:
        print(f"  Skipped: {exc}")

    print("\n" + "=" * 60)
    print("Evaluation complete.")


if __name__ == "__main__":
    main()
