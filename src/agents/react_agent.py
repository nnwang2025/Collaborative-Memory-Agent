"""
ReAct (Reasoning + Acting) Agent for the Collaborative Memory system.

Implements a Think-Act-Observe loop using text-based tool selection
(compatible with any LLM, no native function-calling API required).

Pipeline:
  1. LLM analyzes the question and selects the most relevant tool + query
  2. Agent executes the tool and collects evidence
  3. LLM reviews the evidence and decides: answer now, or try another tool
  4. Repeat until confident (max 5 iterations)
  5. Synthesize final evidence-based answer

Key difference from CoordinatorAgent (linear pipeline):
  - CoordinatorAgent: route → retrieve → answer (one pass, fixed routing)
  - ReActAgent: think → act → observe → think → ... → answer (adaptive)
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from src.agents.answer_agent import AnswerAgent
from src.skills.registry import SkillRegistry


# ---------------------------------------------------------------------------
# Tool definitions (text-based)
# ---------------------------------------------------------------------------

TOOL_DESCRIPTIONS = """\
可用工具（调用时输出 JSON）：
1. search_memory  - 检索历史讨论、技术决策、长期记忆
2. search_issues  - 检索 Issue/Bug/日志/故障记录
3. analyze_dependency - 分析代码依赖、调用链、模块关系
4. search_docs    - 检索架构文档、技术方案、设计说明
5. find_entrypoint - 查找项目入口、启动流程"""

_TOOL_SKILL_MAP = {
    "search_memory": "memory",
    "search_issues": "problem",
    "analyze_dependency": "dependency",
    "search_docs": "document",
    "find_entrypoint": "entrypoint",
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

REACT_SYSTEM_PROMPT = f"""\
你是研发协作记忆系统的 ReAct Agent。你可以使用工具检索研发数据。

{TOOL_DESCRIPTIONS}

工作流程：
1. 分析问题，选择最合适的工具
2. 输出 JSON 调用工具，格式：
   {{"action": "tool_call", "tool": "工具名", "query": "检索查询"}}
3. 收到工具返回的证据后，判断是否需要更多信息
   - 如果证据足够：{{"action": "answer", "summary": "基于证据的回答"}}
   - 如果需要更多信息：{{"action": "tool_call", "tool": "另一个工具", "query": "新查询"}}

规则：
- 每次只调用一个工具
- 最多调用 5 次
- 答案必须基于检索证据，证据不足时明确说明
- 用中文回答

示例问题：为什么采用混合检索？
示例第一步：{{"action": "tool_call", "tool": "search_memory", "query": "为什么采用混合检索 技术决策"}}
（收到证据后）
示例第二步：{{"action": "answer", "summary": "采用混合检索的原因：1. 研发问题同时包含精确实体和模糊语义；2. BM25适合关键词匹配；3. Embedding适合语义查询；4. Rerank综合排序提升准确性。"}}
"""


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

class ToolExecutor:
    """Executes tool calls by delegating to the corresponding Skill."""

    def __init__(self, registry: Optional[SkillRegistry] = None):
        self.registry = registry or SkillRegistry()

    def execute(self, tool_name: str, query: str) -> dict:
        """Run a tool and return structured results."""
        skill_name = _TOOL_SKILL_MAP.get(tool_name)
        if not skill_name:
            return {"error": f"Unknown tool: {tool_name}", "evidence": [], "sources": []}

        skill = self.registry.get(skill_name)
        result = skill.run(query)

        evidence_items = result.get("evidence", [])[:5]
        return {
            "tool": tool_name,
            "skill": skill_name,
            "query": query,
            "evidence": [
                {
                    "source": item.get("source", "unknown"),
                    "content": item.get("content", "")[:300],
                    "score": item.get("score", 0),
                }
                for item in evidence_items
            ],
            "sources": result.get("sources", []),
        }


# ---------------------------------------------------------------------------
# ReAct Agent
# ---------------------------------------------------------------------------

class ReActAgent:
    """ReAct agent with text-based tool selection.

    Parameters
    ----------
    max_iterations:
        Maximum Think-Act-Observe cycles (default 5).
    """

    def __init__(self, max_iterations: int = 5):
        self.max_iterations = max_iterations
        self.executor = ToolExecutor()
        self.answer_agent = AnswerAgent()

        # Pre-warm hybrid retriever (model before faiss – avoids segfault)
        try:
            from src.agents.coordinator_agent import CoordinatorAgent
            CoordinatorAgent._warm_hybrid_retriever()
        except Exception:
            pass

        # LLM config
        self.api_key = (
            os.getenv("SILICONFLOW_API_KEY", "")
            or os.getenv("OPENAI_API_KEY", "")
        ).strip()
        self.model = (
            os.getenv("SILICONFLOW_MODEL", "")
            or os.getenv("MODEL_NAME", "")
            or "Qwen/Qwen2.5-14B-Instruct"
        ).strip()
        self.base_url = (
            os.getenv("SILICONFLOW_BASE_URL", "")
            or os.getenv("OPENAI_BASE_URL", "")
            or "https://api.siliconflow.cn/v1"
        ).strip()

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    def _call_llm(self, messages: List[dict]) -> str:
        """Call the LLM and return the text response."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 512,
        }

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))

        choices = data.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""

    # ------------------------------------------------------------------
    # JSON parsing (robust – handles various model output quirks)
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_action(text: str) -> dict:
        """Extract a JSON action from LLM output.

        Returns a dict with at least ``{"action": "..."}``.
        Returns ``{"action": "error"}`` if parsing fails.
        """
        # Strip markdown fences
        clean = text.strip()
        if clean.startswith("```"):
            lines = clean.split("\n")
            if len(lines) >= 3:
                clean = "\n".join(lines[1:-1]).strip()

        # Try direct JSON parse
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in text
        match = re.search(r'\{[^{}]*"action"\s*:\s*"[^"]+"[^{}]*\}', clean)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        return {"action": "error", "raw": text[:200]}

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, question: str) -> str:
        """Execute the ReAct loop and return the final answer."""
        if not self.api_key:
            return self._fallback_linear(question)

        messages: List[dict] = [
            {"role": "system", "content": REACT_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]

        all_evidence: List[dict] = []
        all_sources: List[str] = []
        tool_calls_made: List[str] = []

        for iteration in range(1, self.max_iterations + 1):
            try:
                response_text = self._call_llm(messages)
            except (urllib.error.URLError, TimeoutError, OSError):
                break  # API error – synthesize what we have

            if not response_text:
                break

            action = self._parse_action(response_text)

            if action.get("action") == "tool_call":
                tool_name = action.get("tool", "")
                query = action.get("query", question)

                if not tool_name or tool_name not in _TOOL_SKILL_MAP:
                    # Invalid tool – ask model to retry
                    messages.append({
                        "role": "assistant",
                        "content": response_text,
                    })
                    messages.append({
                        "role": "user",
                        "content": f"无效工具名。{TOOL_DESCRIPTIONS}\n请重新选择。",
                    })
                    continue

                # --- ACT ---
                tool_result = self.executor.execute(tool_name, query)

                # --- OBSERVE ---
                evidence_count = len(tool_result.get("evidence", []))
                tool_calls_made.append(f"{tool_name}({query[:30]}...)")

                # Build observation message
                obs_parts = [f"工具 {tool_name} 返回了 {evidence_count} 条证据："]
                for i, item in enumerate(tool_result.get("evidence", []), 1):
                    obs_parts.append(
                        f"{i}. [{item['source']}] {item['content'][:200]}"
                    )
                observation = "\n".join(obs_parts)

                # Collect evidence
                for item in tool_result.get("evidence", []):
                    item["tool"] = tool_name
                    all_evidence.append(item)
                all_sources.extend(tool_result.get("sources", []))

                messages.append({
                    "role": "assistant",
                    "content": response_text,
                })
                messages.append({
                    "role": "user",
                    "content": observation
                    + "\n\n如果证据足够，请输出 answer 动作。否则调用另一个工具。",
                })

            elif action.get("action") == "answer":
                summary = action.get("summary", "")
                if summary:
                    return self._format_answer(
                        question, summary, all_evidence, all_sources,
                        iteration, tool_calls_made,
                    )
                # Empty summary – continue
                messages.append({
                    "role": "assistant",
                    "content": response_text,
                })
                messages.append({
                    "role": "user",
                    "content": "请给出具体的回答内容。",
                })

            else:
                # Unrecognized action – nudge the model
                messages.append({
                    "role": "assistant",
                    "content": response_text,
                })
                messages.append({
                    "role": "user",
                    "content": (
                        "请输出 JSON 动作：\n"
                        '{"action": "tool_call", "tool": "工具名", "query": "查询"}\n'
                        '或 {"action": "answer", "summary": "基于证据的回答"}'
                    ),
                })

        # Max iterations reached – synthesize
        return self._synthesize(question, all_evidence, all_sources, tool_calls_made)

    # ------------------------------------------------------------------
    # Output formatting
    # ------------------------------------------------------------------

    def _format_answer(
        self,
        question: str,
        llm_summary: str,
        evidence: List[dict],
        sources: List[str],
        iterations: int,
        tool_calls: List[str],
    ) -> str:
        """Format the final structured answer."""
        lines = [
            f"【回答】(ReAct · {iterations}步 · 调用: {', '.join(tool_calls) or '无'})",
            llm_summary,
        ]

        if evidence:
            lines.append("")
            lines.append("【关键证据】")
            for i, item in enumerate(evidence[:8], 1):
                src = item.get("source", "?")
                score = item.get("score", 0)
                content = item.get("content", "")[:200]
                lines.append(
                    f"- [{item.get('tool', '?')} score={score}] {src}: {content}"
                )

        if sources:
            lines.append("")
            lines.append("【来源文件】")
            for s in sorted(set(sources))[:10]:
                lines.append(f"- {s}")

        return "\n".join(lines)

    def _synthesize(
        self,
        question: str,
        evidence: List[dict],
        sources: List[str],
        tool_calls: List[str],
    ) -> str:
        """Synthesize answer from collected evidence (LLM didn't produce answer)."""
        if not evidence:
            return f"【回答】(ReAct)\n未检索到相关证据。已尝试工具: {', '.join(tool_calls) or '无'}"

        skill_results = {
            "react": {
                "evidence": evidence,
                "sources": sorted(set(sources)),
                "entities": [],
                "answer": f"ReAct Agent 经 {len(tool_calls)} 步收集的证据",
            }
        }
        base = self.answer_agent.compose(
            question, ["react"], skill_results, {"react": len(evidence)}
        )
        # Prepend ReAct metadata
        header = f"【回答】(ReAct · {len(tool_calls)}步 · 调用: {', '.join(tool_calls)})"
        return base.replace("【回答】", header, 1)

    def _fallback_linear(self, question: str) -> str:
        """Fall back to the LangGraph pipeline when no LLM is available."""
        from src.agents.coordinator_agent import CoordinatorAgent
        agent = CoordinatorAgent(use_langgraph=True)
        return agent.ask(question)
