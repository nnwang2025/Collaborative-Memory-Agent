import json
import os
import re
import urllib.error
import urllib.request


class AnswerAgent:
    """Compose final answers from one or more skill results.

    SiliconFlow is used only for Evidence -> Summary. Route, retrieval and skill
    selection stay outside this class.
    """

    SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
    DEFAULT_SILICONFLOW_MODEL = "Qwen/Qwen2.5-14B-Instruct"
    RETRIEVAL_ENTITIES = [
        "Embedding Retrieval",
        "BM25",
        "Rerank",
        "Entity Retrieval",
        "Exact Match",
    ]

    def __init__(self):
        self.siliconflow_api_key = os.getenv("SILICONFLOW_API_KEY", "").strip()
        self.siliconflow_model = os.getenv("SILICONFLOW_MODEL", self.DEFAULT_SILICONFLOW_MODEL).strip()

    def _clean(self, text):
        return str(text or "").replace("\u2011", "-")

    def _compact(self, text, limit=240):
        content = " ".join(self._clean(text).split())
        return content[:limit] + "..." if len(content) > limit else content

    def _contains_any(self, text, words):
        lower = self._clean(text).lower()
        return any(word.lower() in lower for word in words)

    def _question_type(self, question):
        if self._is_optimization_question(question):
            return "optimization"
        if self._contains_any(question, ["谁修改", "修改过", "谁处理", "谁改"]):
            return "owner_change"
        if self._contains_any(question, ["怎么修", "如何修", "修复", "bug", "故障"]):
            return "fix"
        if self._contains_any(question, ["怎么被调用", "调用链", "执行流程", "被谁调用"]):
            return "call_chain"
        if self._contains_any(question, ["为什么", "原因", "采用", "方案", "技术选型", "决策", "没有采用"]):
            return "decision"
        return "general"

    def _matched_retrieval_entity(self, question):
        q = self._clean(question).lower()
        for entity in self.RETRIEVAL_ENTITIES:
            if entity.lower() in q:
                return entity
        if "embedding" in q:
            return "Embedding Retrieval"
        if "entity" in q and "retrieval" in q:
            return "Entity Retrieval"
        if "exact" in q:
            return "Exact Match"
        return None

    def _is_optimization_question(self, question):
        return self._contains_any(question, ["做过哪些优化", "优化了什么", "改进了什么", "调整了什么"])

    def _top_relevant(self, question, evidence):
        q = self._clean(question)
        hints = []
        for word in [
            "agent",
            "rag",
            "bm25",
            "embedding",
            "rerank",
            "memory",
            "langgraph",
            "skill",
            "混合检索",
            "长期记忆",
            "实体抽取",
            "prompt",
            "memoryskill",
            "memory_skill",
        ]:
            if word.lower() in q.lower():
                hints.append(word)

        ranked = []
        for item in evidence:
            content = self._clean(item.get("content", ""))
            score = item.get("score", 0)
            bonus = sum(20 for hint in hints if hint.lower() in content.lower())
            if "decision_memory" in item.get("source", ""):
                bonus += 15
            if "commit" in content.lower() or re.search(r"\bcma\d{4}\b", content.lower()):
                bonus += 5
            ranked.append((bonus + score, item))
        return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)]

    def _evidence_for_prompt(self, evidence):
        lines = []
        for index, item in enumerate(evidence[:8], start=1):
            source = self._clean(item.get("source", "unknown"))
            skill = self._clean(item.get("skill", "unknown"))
            score = item.get("score", 0)
            content = self._compact(item.get("content", ""), limit=700)
            lines.append(f"{index}. source={source}; skill={skill}; score={score}; content={content}")
        return "\n".join(lines)

    def _build_llm_prompt(self, question, evidence):
        qtype = self._question_type(question)
        style_map = {
            "optimization": "如果问题是“做过哪些优化”，总结优化对象、优化原因、具体动作、影响范围、涉及文件。",
            "decision": "如果问题是“为什么采用xx”，总结技术决策原因、替代方案和取舍。",
            "fix": "如果问题是“怎么修复”，按“问题现象、根因、修复方案”总结。",
            "owner_change": "如果问题是“谁修改过”，直接输出人名、Commit、文件。",
            "call_chain": "如果问题是“调用链”，输出清晰调用路径。",
            "general": "输出简洁中文总结，突出与研发协作长期记忆相关的结论。",
        }
        return (
            "你是研发协作记忆系统的总结模块。\n"
            "只能根据给定 evidence 回答，禁止编造不存在的信息。\n"
            "如果证据不足，输出：当前证据不足。\n"
            f"问题类型：{qtype}\n"
            f"总结风格：{style_map[qtype]}\n\n"
            f"用户问题：{self._clean(question)}\n\n"
            "evidence:\n"
            f"{self._evidence_for_prompt(evidence)}\n\n"
            "请只输出【回答】部分的正文，不要输出标题，不要重复列证据。"
        )

    def _summarize_with_siliconflow(self, question, evidence):
        if not self.siliconflow_api_key or not evidence:
            return None

        payload = {
            "model": self.siliconflow_model or self.DEFAULT_SILICONFLOW_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "你是研发协作记忆系统的总结模块，只能根据 evidence 总结。",
                },
                {"role": "user", "content": self._build_llm_prompt(question, evidence)},
            ],
            "temperature": 0.1,
            "max_tokens": 512,
        }
        request = urllib.request.Request(
            f"{self.SILICONFLOW_BASE_URL}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.siliconflow_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError):
            return None

        choices = data.get("choices") or []
        if not choices:
            return None
        content = choices[0].get("message", {}).get("content", "")
        content = self._clean(content).strip()
        return content or None

    def _decision_items(self, evidence):
        return [item for item in evidence if "decision_memory" in item.get("source", "")]

    def _parse_decision_content(self, content):
        text = self._clean(content)
        fields = {}
        for key in ("title", "decision", "alternatives", "reason", "future_plan", "related_entities"):
            match = re.search(rf"{key}:\s*(.*?)(?:\s*\|\s*[a-z_]+:|$)", text, re.IGNORECASE)
            if match:
                fields[key] = match.group(1).strip()
        return fields

    def _score_decision_for_question(self, question, item):
        q = self._clean(question).lower()
        metadata = item.get("metadata") or {}
        text = " ".join([
            self._clean(metadata.get("title", "")),
            self._clean(metadata.get("decision", "")),
            self._clean(" ".join(metadata.get("related_entities", []) if isinstance(metadata.get("related_entities"), list) else [])),
            self._clean(item.get("content", "")),
        ]).lower()

        score = 0
        for token in ["混合检索", "bm25", "embedding", "rerank", "agent", "rag", "memory", "长期记忆", "decision memory"]:
            if token in q and token in text:
                score += 10
        for char in set(q):
            if "\u4e00" <= char <= "\u9fff" and char in text:
                score += 1
        return score + item.get("score", 0)

    def _select_decision_item(self, question, evidence):
        items = self._decision_items(evidence)
        if not items:
            return None
        return max(items, key=lambda item: self._score_decision_for_question(question, item))

    def _decision_summary_from_item(self, item):
        metadata = item.get("metadata") or {}
        parsed = self._parse_decision_content(item.get("content", ""))

        decision = metadata.get("decision") or parsed.get("decision", "")
        alternatives = metadata.get("alternatives") or parsed.get("alternatives", "")
        reason = metadata.get("reason") or parsed.get("reason", "")
        future_plan = metadata.get("future_plan") or parsed.get("future_plan", "")

        if isinstance(reason, str):
            reasons = [part.strip() for part in re.split(r"[；;、]", reason) if part.strip()]
        else:
            reasons = [self._clean(part).strip() for part in reason if self._clean(part).strip()]

        alternative_text = alternatives.strip() if isinstance(alternatives, str) else "、".join(self._clean(part) for part in alternatives)
        if not decision and not reasons:
            return None

        lines = []
        if decision:
            lines.append(f"{decision}。")
        if reasons:
            lines.append("")
            lines.append("采用该方案的原因：")
            for index, item_reason in enumerate(reasons, start=1):
                lines.append(f"{index}. {item_reason}")
        if alternative_text:
            lines.append("")
            lines.append(f"相比 {alternative_text}：")
            if self._contains_any(decision + " " + " ".join(reasons), ["混合检索", "BM25", "Embedding", "Rerank"]):
                lines.append("更适合研发协作场景中的实体 + 语义混合问题。")
            else:
                lines.append("当前方案更符合 Collaborative Memory Agent 的 MVP 目标和可解释性要求。")
        if future_plan:
            lines.append("")
            lines.append(f"后续计划：{future_plan}")
        return "\n".join(lines)

    def _extract_decisions(self, evidence):
        decisions = []
        for item in self._decision_items(evidence):
            metadata = item.get("metadata") or {}
            decision = metadata.get("decision")
            if not decision:
                decision = self._parse_decision_content(item.get("content", "")).get("decision")
            if decision:
                decisions.append(self._clean(decision).strip())
        return list(dict.fromkeys(decisions))

    def _fallback_optimization_summary(self, question, evidence):
        entity = self._matched_retrieval_entity(question)
        if not entity:
            return None

        text = "\n".join(self._clean(item.get("content", "")) for item in evidence)
        files = re.findall(r"(?:src|docs|data)/[a-zA-Z0-9_./-]+\.(?:py|md|json)", text)

        if entity == "Embedding Retrieval":
            points = [
                "用于处理模糊自然语言和语义相似问题。",
                "与 Exact Match、BM25、Entity Retrieval 一起组成混合检索，避免纯向量检索漏掉文件名、函数名、Issue ID 等精确实体。",
                "通过 Rerank 在召回后结合实体重合度、来源可信度和上下文相关性重新排序。",
                "补充 mock 数据、技术文档和 Skill 逻辑，使 Embedding Retrieval 的检索与回答更稳定。",
            ]
        elif entity == "BM25":
            points = [
                "保留 BM25 用于关键词、中文短文本和 Commit Message 召回。",
                "与 Exact Match、Embedding Retrieval、Entity Retrieval 组合，弥补纯语义检索对精确词的遗漏。",
                "通过 Rerank 对 BM25 与其他召回结果统一排序，提升最终证据质量。",
            ]
        elif entity == "Rerank":
            points = [
                "用于对多路召回结果进行最终重排。",
                "排序时综合实体重合度、来源可信度和上下文相关性。",
                "配合 Decision Memory 和实体命中权重，减少无关聊天或低相关片段进入最终答案。",
            ]
        elif entity == "Entity Retrieval":
            points = [
                "根据 Agent、Skill、模块、函数、Issue、Commit 和技术关键词召回证据。",
                "当问题出现实体名称时提高实体命中权重。",
                "优先召回 decision_memory、docs、issues、commits 中与实体相关的记录。",
            ]
        else:
            points = [
                "用于精确命中文件名、函数名、Issue ID、Commit Hash 和错误片段。",
                "与 BM25、Embedding Retrieval、Entity Retrieval 组合，覆盖精确实体和模糊语义两类问题。",
            ]

        lines = [f"{entity} 的优化主要包括："]
        lines.extend(f"{index}. {point}" for index, point in enumerate(points, start=1))
        if files:
            lines.append("")
            lines.append("涉及文件：" + "、".join(dict.fromkeys(files)))
        return "\n".join(lines)

    def _fallback_decision_summary(self, question, evidence):
        decisions = self._extract_decisions(evidence)
        if self._contains_any(question, ["哪些", "历史上", "所有", "列表"]) and decisions:
            return "历史上沉淀的关键技术决策包括：" + "；".join(decisions) + "。"

        selected = self._select_decision_item(question, evidence)
        if selected:
            summary = self._decision_summary_from_item(selected)
            if summary:
                return summary

        return "根据命中的证据，相关方案主要围绕 Agent 架构、混合检索、长期记忆和回答证据化展开。"

    def _fallback_owner_change_summary(self, question, evidence):
        text = "\n".join(self._clean(item.get("content", "")) for item in self._top_relevant(question, evidence)[:3])
        commit_match = re.search(r"\b(cma\d{4})\b", text.lower())
        author_match = re.search(r"\b(lin|zhou|chen|wang|li)\b", text.lower())
        files = re.findall(r"(?:src|docs|data)/[a-zA-Z0-9_./-]+\.(?:py|md|json)", text)

        parts = [f"{author_match.group(1) if author_match else '相关开发者'} 修改过该项目模块"]
        if commit_match:
            parts.append(f"commit 是 {commit_match.group(1)}")
        if files:
            parts.append("涉及文件包括 " + "、".join(dict.fromkeys(files)))
        return "，".join(parts) + "。"

    def _fallback_fix_summary(self, question, evidence):
        text = "\n".join(self._clean(item.get("content", "")) for item in self._top_relevant(question, evidence)[:4])
        files = re.findall(r"(?:src|docs|data)/[a-zA-Z0-9_./-]+\.(?:py|md|json)", text)
        phenomenon = "问题现象：项目检索、路由或回答质量在某些研发协作问题上不稳定。"
        root = "根因：需要结合 Issue、日志和 Commit 证据确认具体模块。"
        fix = "修复方式：根据命中的记录调整 Skill 逻辑、mock 数据、检索权重或回答模板。"

        if "route_score" in text or "Coordinator" in text:
            phenomenon = "问题现象：问题路由不够稳定，容易把研发记忆问题分到不合适的 Skill。"
            root = "根因：硬编码关键词缺少加权和 Top 2 聚合策略。"
            fix = "修复方式：在 Coordinator Agent 中引入 route_score，并在分差较小时同时调用 Top 2 Skill。"
        elif "Rerank" in text or "BM25" in text or "Embedding" in text:
            phenomenon = "问题现象：检索结果会混入无关聊天或低相关片段。"
            root = "根因：多路召回后缺少足够明确的重排和证据聚焦。"
            fix = "修复方式：提高 Decision Memory、实体命中和项目文档的权重，并用 Rerank 聚合证据。"

        file_part = "涉及文件：" + ("、".join(dict.fromkeys(files)) if files else "见下方来源文件。")
        return "\n".join([phenomenon, root, fix, file_part])

    def _fallback_call_chain_summary(self, evidence):
        text = "\n".join(self._clean(item.get("content", "")) for item in evidence)
        if "CoordinatorAgent.ask" in text and "MemorySkill.run" in text:
            return (
                "memory_skill 的调用路径是：main.py 创建 CoordinatorAgent；"
                "CoordinatorAgent.ask(question) 调用 route(question) 和 route_scores(question) 判断应走 memory；"
                "随后通过 SkillRegistry.get('memory') 取得 MemorySkill 实例；"
                "最终执行 MemorySkill.run(question) 完成长期记忆检索。"
            )
        return "根据 AST 调用链分析，相关函数的调用路径见下方关键证据。"

    def _fallback_summary(self, question, skill_names, evidence):
        qtype = self._question_type(question)
        if qtype == "optimization":
            summary = self._fallback_optimization_summary(question, evidence)
            if summary:
                return summary
        if qtype == "owner_change":
            return self._fallback_owner_change_summary(question, evidence)
        if qtype == "fix":
            return self._fallback_fix_summary(question, evidence)
        if qtype == "call_chain":
            return self._fallback_call_chain_summary(evidence)
        if qtype == "decision":
            return self._fallback_decision_summary(question, evidence)
        if "memory" in skill_names and any("decision_memory" in item.get("source", "") for item in evidence):
            return self._fallback_decision_summary(question, evidence)
        return "根据命中的证据，已整理出与 Collaborative Memory Agent 相关的结论和来源；详细片段见下方关键证据。"

    def _summary(self, question, skill_names, evidence):
        if self._is_optimization_question(question) and self._matched_retrieval_entity(question):
            return self._fallback_summary(question, skill_names, evidence)
        llm_summary = self._summarize_with_siliconflow(question, evidence)
        if llm_summary:
            return llm_summary
        return self._fallback_summary(question, skill_names, evidence)

    def compose(self, question, skill_names, skill_results, route_scores=None):
        if isinstance(skill_names, str):
            skill_names = [skill_names]
        if isinstance(skill_results, dict) and "evidence" in skill_results:
            skill_results = {skill_names[0]: skill_results}

        route_scores = route_scores or {}
        evidence = []
        sources = set()
        entities = set()

        for skill_name in skill_names:
            result = skill_results.get(skill_name, {})
            for item in result.get("evidence", []):
                copied = dict(item)
                copied["skill"] = skill_name
                evidence.append(copied)
            sources.update(result.get("sources", []))
            entities.update(result.get("entities", []))

        evidence = self._top_relevant(question, evidence)[:8]
        summary = self._summary(question, skill_names, evidence)
        if evidence:
            sources = {item.get("source", "unknown") for item in evidence}

        evidence_lines = []
        for item in evidence:
            source = item.get("source", "unknown")
            score = item.get("score", 0)
            skill_name = item.get("skill", "unknown")
            content = self._compact(item.get("content", ""))
            evidence_lines.append(f"- [{skill_name} score={score}] {source}: {content}")

        source_lines = [f"- {source}" for source in sorted(sources)]
        entity_lines = [f"- {entity}" for entity in sorted(entities)]
        max_score = max(route_scores.values()) if route_scores else 0
        confidence = "高" if max_score >= 8 else "中" if max_score >= 4 else "低"
        route_lines = [f"- {skill}: {score}" for skill, score in sorted(route_scores.items())]
        route_lines.append(f"- 最终调用: {', '.join(skill_names)}")

        return "\n".join([
            "【回答】",
            summary,
            "",
            "【关键证据】",
            "\n".join(evidence_lines) if evidence_lines else "- 暂无命中证据。",
            "",
            "【来源文件】",
            "\n".join(source_lines) if source_lines else "- 暂无来源文件。",
            "",
            "【关联实体】",
            "\n".join(entity_lines) if entity_lines else "- 暂未识别到明确实体。",
            "",
            "【置信度】",
            confidence,
            "",
            "【路由结果】",
            "\n".join(route_lines) if route_lines else "- 暂无路由分数。",
        ])
