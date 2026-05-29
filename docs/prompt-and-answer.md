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
