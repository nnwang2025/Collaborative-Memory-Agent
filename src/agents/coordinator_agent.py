from src.agents.answer_agent import AnswerAgent
from src.skills.registry import SkillRegistry


class CoordinatorAgent:
    """Score-based Coordinator Agent that routes a question to one or two skills."""

    ROUTE_SIGNALS = {
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
        "agent",
        "rag",
        "bm25",
        "embedding",
        "rerank",
        "memory",
        "langgraph",
        "skill",
        "实体抽取",
        "混合检索",
        "长期记忆",
        "prompt",
        "架构",
        "接口",
        "技术选型",
        "方案",
        "文档",
    ]
    CHANGE_HISTORY_SIGNALS = ["谁修改", "修改过", "谁改", "改过", "commit", "提交"]

    def __init__(self, registry=None, answer_agent=None):
        self.registry = registry or SkillRegistry()
        self.answer_agent = answer_agent or AnswerAgent()

    def route_scores(self, question):
        q = (question or "").lower()
        scores = {skill: 0 for skill in self.ROUTE_SIGNALS}

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

    def route(self, question):
        scores = self.route_scores(question)
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if ranked[0][1] < 2:
            return ["memory", "document"]

        selected = [ranked[0][0]]
        if ranked[1][1] > 0 and ranked[0][1] - ranked[1][1] <= 1:
            selected.append(ranked[1][0])

        q = (question or "").lower()
        is_decision_or_history = any(signal in q for signal in self.DECISION_SIGNALS + self.RETROSPECTIVE_SIGNALS)
        if selected == ["document"] and scores.get("memory", 0) >= 5 and is_decision_or_history:
            selected.append("memory")
        return selected

    def ask(self, question):
        scores = self.route_scores(question)
        skill_names = self.route(question)
        results = {}
        for skill_name in skill_names:
            skill = self.registry.get(skill_name)
            results[skill_name] = skill.run(question)
        return self.answer_agent.compose(question, skill_names, results, scores)
