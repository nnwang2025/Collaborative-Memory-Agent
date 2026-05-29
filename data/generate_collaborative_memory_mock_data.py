import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


topics = [
    ("Agent架构设计", "Coordinator Agent 负责意图识别、route_score 计算和 Skill 聚合。"),
    ("混合检索", "系统同时使用 Exact Match、BM25、Entity Retrieval、Embedding Retrieval 和 Rerank。"),
    ("BM25", "BM25 用于处理中文关键词、Issue 标题、Commit Message 和短文本召回。"),
    ("Embedding Retrieval", "Embedding Retrieval 用于处理模糊自然语言和语义相似问题。"),
    ("Rerank", "Rerank 根据实体重合度、来源可信度和时间线重新排序候选证据。"),
    ("Memory设计", "长期记忆被拆分为 Semantic、Episodic、Entity、Relationship 和 Decision Memory。"),
    ("LangGraph工作流", "后续计划把 Coordinator 的路由与聚合升级成更清晰的 LangGraph workflow。"),
    ("多Skill路由", "memory、problem、dependency、entrypoint、document Skill 分别处理不同研发协作问题。"),
    ("实体抽取", "实体抽取会识别模块、文件、函数、Issue、Commit、技术关键词和项目角色。"),
    ("长期记忆管理", "Memory Manager 统一管理记忆写入、更新、去重、证据绑定和冷启动加载。"),
    ("检索优化", "检索优化重点是降低无关聊天命中，提高决策记忆和项目文档的权重。"),
    ("Prompt设计", "Prompt 需要约束回答先总结再给证据，避免只罗列搜索结果。"),
]


issues = []
for i in range(1, 21):
    topic, desc = topics[(i - 1) % len(topics)]
    issues.append({
        "issue_id": f"CMA-{100 + i}",
        "title": f"{topic}优化：{desc[:24]}",
        "type": ["design", "retrieval", "memory", "workflow"][i % 4],
        "status": "closed" if i <= 16 else "open",
        "module": [
            "coordinator_agent",
            "memory_skill",
            "document_skill",
            "dependency_skill",
            "answer_agent",
            "retrieval",
        ][i % 6],
        "discussion": f"团队讨论了 {topic} 在 Collaborative Memory Agent 中的实现边界。{desc}",
        "decision": f"保留轻量 MVP 架构，围绕 {topic} 做小范围增强，并要求输出保留证据来源。",
        "fix": f"补充 mock 数据、技术文档和 Skill 逻辑，使 {topic} 的检索与回答更稳定。",
        "related_commit": f"cma{i:04d}",
        "related_entities": [topic, "Collaborative Memory Agent", "Agent + RAG"],
    })


commits = []
for i in range(1, 51):
    topic, desc = topics[(i - 1) % len(topics)]
    scope = [
        "coordinator",
        "memory",
        "retrieval",
        "document",
        "dependency",
        "answer",
        "mock-data",
        "workflow",
    ][i % 8]
    author = ["lin", "zhou", "chen", "wang", "li"][i % 5]
    verb = ["feat", "fix", "refactor", "docs", "test"][i % 5]
    files = {
        "coordinator": ["src/agents/coordinator_agent.py"],
        "memory": ["src/skills/memory_skill.py", "data/decision_memory.json"],
        "retrieval": ["src/skills/utils.py", "src/skills/document_skill.py"],
        "document": ["docs/retrieval-design.md", "docs/memory-design.md"],
        "dependency": ["src/skills/dependency_skill.py"],
        "answer": ["src/agents/answer_agent.py"],
        "mock-data": ["data/chats/project_chats.json", "data/issues/project_issues.json"],
        "workflow": ["modules/graph.py", "docs/langgraph-workflow.md"],
    }[scope]
    commits.append({
        "commit": f"cma{i:04d}",
        "author": author,
        "message": f"{verb}({scope}): improve {topic.lower()} for collaborative memory agent",
        "files": files,
        "summary": f"{author} 围绕 {topic} 修改了 {', '.join(files)}。{desc}",
        "related_issue": f"CMA-{100 + ((i - 1) % 20) + 1}",
        "related_entities": [topic, scope, "Collaborative Memory Agent"],
    })


speakers = ["架构师", "后端工程师", "算法工程师", "产品负责人", "测试工程师"]
chats = []
for i in range(1, 101):
    topic, desc = topics[(i - 1) % len(topics)]
    speaker = speakers[i % len(speakers)]
    chats.append({
        "time": f"2026-05-{((i - 1) // 5) + 1:02d} {9 + (i % 9):02d}:{(i * 7) % 60:02d}",
        "speaker": speaker,
        "content": f"{speaker} 讨论 {topic}：{desc} 当前结论是继续保持 Coordinator Agent + 多 Skill 的轻量实现，先用本地 mock 数据验证。",
        "tags": [topic, "Collaborative Memory Agent", "Agent + RAG"],
        "related_issue": f"CMA-{100 + ((i - 1) % 20) + 1}",
    })


logs = []
for i in range(1, 16):
    topic, desc = topics[(i - 1) % len(topics)]
    logs.append({
        "time": f"2026-05-{i:02d} 18:{(i * 3) % 60:02d}:00",
        "level": ["INFO", "WARN", "ERROR"][i % 3],
        "module": ["coordinator", "retrieval", "memory", "answer", "document"][i % 5],
        "message": f"{topic} 回归验证：{desc}",
        "trace": f"CoordinatorAgent.ask -> route_scores -> {['memory_skill', 'document_skill', 'dependency_skill'][i % 3]}.run",
        "resolution": f"通过调整 mock 数据和 evidence 聚合逻辑改善 {topic} 的回答质量。",
    })


decision_memory = [
    {
        "id": "decision-agent-rag-lightweight",
        "title": "采用 Coordinator Agent + 多 Skill 的轻量架构",
        "decision": "Collaborative Memory Agent 采用 Coordinator Agent + 多 Skill + RAG 的轻量 MVP 架构",
        "alternatives": ["复杂多智能体平台", "单一向量库问答", "纯规则检索"],
        "reason": [
            "项目目标是研发协作长期记忆，MVP 阶段需要先保证可运行和可解释",
            "不同问题适合交给不同 Skill，但不需要拆成多个独立 Agent",
            "Coordinator 能统一路由、聚合证据和控制输出格式",
            "本地 mock 数据足以验证核心闭环"
        ],
        "future_plan": "后续可在保持轻量接口的前提下接入 LangGraph workflow 和真实研发系统",
        "related_entities": ["Coordinator Agent", "Skill", "RAG", "MVP", "Collaborative Memory Agent"],
        "source_files": ["docs/architecture.md", "data/chats/project_chats.json"]
    },
    {
        "id": "decision-hybrid-retrieval",
        "title": "采用混合检索",
        "decision": "检索层采用 Exact Match、BM25、Entity Retrieval、Embedding Retrieval 和 Rerank 的混合检索",
        "alternatives": ["纯 Embedding Retrieval", "单一 BM25", "只靠关键词搜索"],
        "reason": [
            "研发问题同时包含精确实体、文件名、函数名、Issue 和模糊自然语言",
            "BM25 适合短文本和关键词召回",
            "Embedding Retrieval 适合模糊语义查询",
            "Rerank 可以综合实体重合度、来源可信度和上下文相关性"
        ],
        "future_plan": "后续引入更稳定的 reranker 与评测集，持续优化召回质量",
        "related_entities": ["BM25", "Embedding Retrieval", "Rerank", "Entity Retrieval", "Exact Match"],
        "source_files": ["docs/retrieval-design.md", "data/issues/project_issues.json"]
    },
    {
        "id": "decision-memory-layering",
        "title": "长期记忆分层设计",
        "decision": "长期记忆采用 Semantic、Episodic、Entity、Relationship 和 Decision Memory 分层",
        "alternatives": ["只保存原始聊天", "只保存文档切片", "单表 memory_records"],
        "reason": [
            "历史事件、技术知识、实体和关系的查询方式不同",
            "Decision Memory 能稳定回答为什么采用某个方案",
            "Entity Memory 支持模块、函数、Issue、Commit 的关联回溯",
            "Relationship Memory 为后续知识图谱和调用链分析预留空间"
        ],
        "future_plan": "后续由 Memory Manager 统一管理写入、去重、更新和证据绑定",
        "related_entities": ["Memory Manager", "Decision Memory", "Entity Memory", "Relationship Memory"],
        "source_files": ["docs/memory-design.md", "data/chats/project_chats.json"]
    },
    {
        "id": "decision-answer-format",
        "title": "回答先总结再给证据",
        "decision": "Answer Agent 输出先给结论总结，再列关键证据、来源文件、关联实体、置信度和路由结果",
        "alternatives": ["只罗列搜索结果", "只生成自然语言结论"],
        "reason": [
            "研发协作场景需要可追溯答案",
            "只罗列证据阅读成本高",
            "只给结论容易缺少可信来源",
            "固定格式便于 CLI、API 和后续 UI 消费"
        ],
        "future_plan": "后续按问题类型继续增强摘要模板和证据压缩策略",
        "related_entities": ["Answer Agent", "Evidence", "Prompt设计", "置信度", "路由结果"],
        "source_files": ["docs/prompt-and-answer.md", "src/agents/answer_agent.py"]
    }
]


entity_memory = [
    {"entity": "Coordinator Agent", "type": "agent", "summary": "负责 route_score、Skill 路由和答案聚合。"},
    {"entity": "MemorySkill", "type": "skill", "summary": "负责历史讨论、长期记忆和 Decision Memory 检索。"},
    {"entity": "DocumentSkill", "type": "skill", "summary": "负责 docs Markdown 的关键词与 BM25 检索。"},
    {"entity": "DependencySkill", "type": "skill", "summary": "负责项目 import、类、函数和 AST 调用链分析。"},
    {"entity": "BM25", "type": "retrieval", "summary": "用于短文本、关键词和中文片段召回。"},
    {"entity": "Embedding Retrieval", "type": "retrieval", "summary": "用于模糊语义检索。"},
    {"entity": "Rerank", "type": "retrieval", "summary": "用于对多路召回结果进行相关性重排。"},
    {"entity": "Memory Manager", "type": "memory", "summary": "统一管理长期记忆分层、更新、去重和证据绑定。"}
]


episodic = [
    {
        "record_id": f"episode-{i:03d}",
        "time_start": f"2026-05-{i:02d} 10:00",
        "time_end": f"2026-05-{i:02d} 11:00",
        "summary": f"团队围绕 {topics[(i - 1) % len(topics)][0]} 完成一次设计讨论，结论是保持轻量 Agent + RAG MVP，并补充对应 mock 数据与证据输出。"
    }
    for i in range(1, 13)
]


semantic_memory = [
    {
        "record_id": f"semantic-{i:03d}",
        "time_start": f"2026-05-{i:02d} 09:00",
        "time_end": f"2026-05-{i:02d} 09:30",
        "participants": ["架构师", "算法工程师"],
        "topic_hints": [topic, "Collaborative Memory Agent"],
        "entity_memory": {"tech": [topic, "Agent + RAG"], "module": ["Coordinator Agent", "Skill"]},
        "summary": f"{topic} 的项目知识沉淀：{desc} 该结论用于 Collaborative Memory Agent 的长期记忆检索、证据聚合和回答生成。"
    }
    for i, (topic, desc) in enumerate(topics, start=1)
]


relationships = [
    {"source": "Coordinator Agent", "relation": "routes_to", "target": "MemorySkill", "evidence": "src/agents/coordinator_agent.py"},
    {"source": "Coordinator Agent", "relation": "routes_to", "target": "DocumentSkill", "evidence": "src/skills/registry.py"},
    {"source": "MemorySkill", "relation": "reads", "target": "Decision Memory", "evidence": "data/decision_memory.json"},
    {"source": "DocumentSkill", "relation": "retrieves_from", "target": "docs/*.md", "evidence": "docs/retrieval-design.md"},
    {"source": "Hybrid Retrieval", "relation": "contains", "target": "BM25", "evidence": "docs/retrieval-design.md"},
    {"source": "Hybrid Retrieval", "relation": "contains", "target": "Embedding Retrieval", "evidence": "docs/retrieval-design.md"},
    {"source": "Hybrid Retrieval", "relation": "contains", "target": "Rerank", "evidence": "docs/retrieval-design.md"},
    {"source": "Answer Agent", "relation": "uses", "target": "Evidence Builder", "evidence": "docs/prompt-and-answer.md"}
]


docs = {
    "docs/architecture.md": """
# Collaborative Memory Agent 架构设计

Collaborative Memory Agent 是一个基于 Agent + RAG 的研发协作长期记忆系统。MVP 采用一个 Coordinator Agent 加多个 Skill 的轻量架构，而不是复杂的多智能体平台。

Coordinator Agent 负责对用户问题计算 route_score，并选择 memory、problem、dependency、entrypoint 或 document Skill。Answer Agent 负责聚合证据，输出回答、关键证据、来源文件、关联实体、置信度和路由结果。

设计原则：

- 优先保证本地 mock 数据可运行。
- 通过多 Skill 分工覆盖研发协作场景。
- 通过证据化输出降低幻觉风险。
- 为 LangGraph workflow 和真实研发系统接入预留扩展点。
""",
    "docs/retrieval-design.md": """
# 混合检索设计

系统采用混合检索，而不是只依赖单一路径。Exact Match 用于命中文件名、Issue ID、Commit Hash 和函数名。BM25 用于中文关键词、短文本和 Commit Message 召回。Embedding Retrieval 用于模糊语义查询。Entity Retrieval 用于识别 Agent、Skill、模块、函数、Issue、Commit 和技术关键词。Rerank 用于综合实体重合度、来源可信度和上下文相关性。

典型问题：

- 之前讨论过 BM25 和 Embedding 怎么融合吗？
- Rerank 为什么放在召回之后？
- 检索优化做过哪些调整？
""",
    "docs/memory-design.md": """
# 长期记忆设计

长期记忆分为 Semantic Memory、Episodic Memory、Entity Memory、Relationship Memory 和 Decision Memory。

Semantic Memory 保存沉淀后的项目知识。Episodic Memory 保存历史事件和讨论过程。Entity Memory 保存 Agent、Skill、模块、文件、函数和技术关键词。Relationship Memory 保存实体之间的关系。Decision Memory 专门记录技术决策、替代方案、原因和后续计划。

Memory Manager 后续负责统一写入、去重、更新、冷启动加载和证据绑定。
""",
    "docs/langgraph-workflow.md": """
# LangGraph 工作流规划

当前 MVP 使用轻量 Coordinator Agent 完成路由和聚合。后续可以把路由、检索、证据聚合和答案生成拆成 LangGraph 节点。

规划节点：

- classify_query：识别问题类型。
- route_skill：计算 route_score 并选择 Skill。
- retrieve_evidence：执行 Skill 检索。
- merge_evidence：合并、去重、排序证据。
- compose_answer：先总结，再给证据。

该设计保持当前 Skill 接口不变，避免大规模重构。
""",
    "docs/prompt-and-answer.md": """
# Prompt 与回答格式设计

Answer Agent 的目标不是简单罗列搜索结果，而是先给用户一个简洁结论，再展示证据。

固定输出格式：

【回答】
先总结结论。

【关键证据】
列出命中的证据片段。

【来源文件】
列出 docs、data、src 等来源。

【关联实体】
列出 Agent、Skill、BM25、Embedding Retrieval、Rerank、Memory Manager 等实体。

【置信度】
根据 route_score 和证据质量给出高、中、低。

【路由结果】
列出每个 Skill 的分数和最终调用的 Skill。
""",
}


def main():
    write_json(ROOT / "data/chats/project_chats.json", chats)
    write_json(ROOT / "data/issues/project_issues.json", issues)
    write_json(ROOT / "data/commits/project_commits.json", commits)
    write_json(ROOT / "data/logs/project_logs.json", logs)
    write_json(ROOT / "data/decision_memory.json", decision_memory)
    write_json(ROOT / "data/memory_records.json", semantic_memory)
    write_json(ROOT / "data/entity_memory.json", entity_memory)
    write_json(ROOT / "data/episodic_summaries.json", episodic)
    write_json(ROOT / "data/relationship_memory.json", relationships)

    for path, content in docs.items():
        write_text(ROOT / path, content)


if __name__ == "__main__":
    main()
