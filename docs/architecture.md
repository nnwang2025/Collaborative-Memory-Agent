# Collaborative Memory Agent 架构设计

Collaborative Memory Agent 是一个基于 Agent + RAG 的研发协作长期记忆系统。MVP 采用一个 Coordinator Agent 加多个 Skill 的轻量架构，而不是复杂的多智能体平台。

Coordinator Agent 负责对用户问题计算 route_score，并选择 memory、problem、dependency、entrypoint 或 document Skill。Answer Agent 负责聚合证据，输出回答、关键证据、来源文件、关联实体、置信度和路由结果。

设计原则：

- 优先保证本地 mock 数据可运行。
- 通过多 Skill 分工覆盖研发协作场景。
- 通过证据化输出降低幻觉风险。
- 为 LangGraph workflow 和真实研发系统接入预留扩展点。
