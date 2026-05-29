import json
from pathlib import Path

INPUT_PATH = Path("data/md/chat_memory.json")
OUTPUT_PATH = Path("data/memory_records.json")


def load_chats():
    with open(INPUT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_blocks(chats, window_size=6, step=3):
    memory_records = []

    # 关键词和实体词表（可扩展）
    KEYWORDS = ["项目", "实习", "数据库", "bug", "深圳", "任务", "方向", "决策", "方案", "定位", "焦虑", "问题", "技术", "日志", "会议", "总结"]
    ENTITY_TYPES = {
        "location": ["深圳", "北京", "上海", "广州"],
        "project": ["MiniDevAgent", "RAG", "Agent", "项目"],
        "task": ["任务", "开发", "优化", "测试", "修复"],
        "problem": ["bug", "问题", "报错", "失败", "焦虑"],
        "technology": ["数据库", "BM25", "向量", "LangGraph", "OpenAI", "embedding", "日志"]
    }

    def extract_topic_hints(block):
        text = " ".join([msg.get("content", "") for msg in block])
        return list({kw for kw in KEYWORDS if kw in text})

    def extract_entity_memory(block):
        text = " ".join([msg.get("content", "") for msg in block])
        entity_memory = {}
        for etype, candidates in ENTITY_TYPES.items():
            hits = [c for c in candidates if c in text]
            if hits:
                entity_memory[etype] = hits
        return entity_memory

    for i in range(0, len(chats), step):
        block = chats[i:i + window_size]
        if not block:
            continue
        time_start = block[0].get("time", "")
        time_end = block[-1].get("time", "")
        speakers = list({msg.get("speaker", "") for msg in block})
        raw_text = "\n".join([
            f"[{msg.get('time', '')}] {msg.get('speaker', '')}: {msg.get('content', '')}"
            for msg in block
        ])
        topic_hints = extract_topic_hints(block)
        entity_memory = extract_entity_memory(block)
        record = {
            "record_id": len(memory_records),
            "time_start": time_start,
            "time_end": time_end,
            "participants": speakers,
            "topic_hints": topic_hints,
            "entity_memory": entity_memory,
            "summary": raw_text,
            "raw_messages": block
        }
        memory_records.append(record)
    return memory_records


def main():
    chats = load_chats()
    print(f"loaded chats: {len(chats)}")

    records = build_blocks(chats)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"built {len(records)} memory records -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()