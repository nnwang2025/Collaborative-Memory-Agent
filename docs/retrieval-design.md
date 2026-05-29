# 混合检索设计

系统采用混合检索，而不是只依赖单一路径。Exact Match 用于命中文件名、Issue ID、Commit Hash 和函数名。BM25 用于中文关键词、短文本和 Commit Message 召回。Embedding Retrieval 用于模糊语义查询。Entity Retrieval 用于识别 Agent、Skill、模块、函数、Issue、Commit 和技术关键词。Rerank 用于综合实体重合度、来源可信度和上下文相关性。

典型问题：

- 之前讨论过 BM25 和 Embedding 怎么融合吗？
- Rerank 为什么放在召回之后？
- 检索优化做过哪些调整？
