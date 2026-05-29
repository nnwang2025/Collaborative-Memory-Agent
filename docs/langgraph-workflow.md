# LangGraph 工作流规划

当前 MVP 使用轻量 Coordinator Agent 完成路由和聚合。后续可以把路由、检索、证据聚合和答案生成拆成 LangGraph 节点。

规划节点：

- classify_query：识别问题类型。
- route_skill：计算 route_score 并选择 Skill。
- retrieve_evidence：执行 Skill 检索。
- merge_evidence：合并、去重、排序证据。
- compose_answer：先总结，再给证据。

该设计保持当前 Skill 接口不变，避免大规模重构。
