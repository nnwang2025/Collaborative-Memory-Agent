import json
from datetime import datetime, timedelta
from pathlib import Path
import random

topics = [
    ("internship", "我想找深圳AI应用开发实习。", "深圳AI应用岗位更偏Agent、RAG和工程落地。"),
    ("project", "我担心项目太toy。", "项目重点是Workflow、Memory、Retrieval和工程化。"),
    ("rag", "RAG到底是什么？", "RAG是先检索相关内容，再让大模型基于上下文回答。"),
    ("agent", "Agent和RAG有什么区别？", "Agent更强调决策、工具调用、记忆管理和任务流程。"),
    ("reranker", "Reranker是不是必须加？", "第一版可以不加，后续用BGE-Reranker优化Top-K排序。"),
    ("leetcode", "我力扣刷得很慢怎么办？", "先刷Hot100高频题，重点掌握模板和思路复述。"),
    ("backend", "AI应用岗会问后端吗？", "会问FastAPI、HTTP、Redis、Docker、接口设计，但不会像Java后端那么深。"),
    ("resume", "简历项目怎么写才不像套壳？", "要写清楚问题、方案、技术链路和可量化优化点。"),
    ("emotion", "我感觉自己代码能力一般。", "先跑通项目，再逐步理解核心模块，不要一开始追求全懂。"),
    ("plan", "我一个月能做完两个项目吗？", "先集中做第一个Conversation Memory Agent，再做第二个Multi-Agent项目。")
]

records = [
    {
        "time": "2026-04-01 21:30",
        "speaker": "user",
        "topic": "internship",
        "emotion": "anxiety",
        "content": "我准备找深圳的大模型应用开发实习。"
    },
    {
        "time": "2026-04-01 21:31",
        "speaker": "assistant",
        "topic": "internship",
        "emotion": "guidance",
        "content": "你目前更适合从Agent+RAG项目切入。"
    },
    {
        "time": "2026-04-05 22:10",
        "speaker": "user",
        "topic": "project",
        "emotion": "worry",
        "content": "我担心项目太toy，怕面试官觉得它只是普通RAG demo。"
    },
    {
        "time": "2026-04-05 22:11",
        "speaker": "assistant",
        "topic": "project",
        "emotion": "guidance",
        "content": "重点是做出Workflow、Memory、Retrieval和工程化，让项目不像toy。"
    },
    {
        "time": "2026-04-10 20:00",
        "speaker": "user",
        "topic": "reranker",
        "emotion": "curiosity",
        "content": "我之前问过Reranker是不是必须加？"
    },
    {
        "time": "2026-04-10 20:01",
        "speaker": "assistant",
        "topic": "reranker",
        "emotion": "guidance",
        "content": "第一版可以不加，但后续加BGE-Reranker能让Top-K检索结果排序更稳定。"
    }
]
start = datetime(2026, 4, 1, 20, 0)

for i in range(50):
    topic, user_msg, assistant_msg = random.choice(topics)
    t1 = start + timedelta(hours=i * 5)
    t2 = t1 + timedelta(minutes=1)

    records.append({
        "time": t1.strftime("%Y-%m-%d %H:%M"),
        "speaker": "user",
        "topic": topic,
        "emotion": random.choice(["anxiety", "curiosity", "worry", "confusion", "planning"]),
        "content": user_msg
    })

    records.append({
        "time": t2.strftime("%Y-%m-%d %H:%M"),
        "speaker": "assistant",
        "topic": topic,
        "emotion": "guidance",
        "content": assistant_msg
    })

Path("data").mkdir(exist_ok=True)

with open("data/chat_raw.json", "w", encoding="utf-8") as f:
    json.dump(records, f, ensure_ascii=False, indent=2)

print(f"generated {len(records)} chat records -> data/chat_raw.json")