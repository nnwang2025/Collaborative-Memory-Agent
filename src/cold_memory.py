from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SEMANTIC_MEMORY_FILE = ROOT / "data" / "memory_records.json"
EPISODIC_MEMORY_FILE = ROOT / "data" / "episodic_summaries.json"
ENTITY_MEMORY_FILE = ROOT / "data" / "entity_memory.json"
RELATIONSHIP_MEMORY_FILE = ROOT / "data" / "relationship_memory.json"


def cold_memory_files():
    return {
        "semantic_memory": SEMANTIC_MEMORY_FILE,
        "episodic_memory": EPISODIC_MEMORY_FILE,
        "entity_memory": ENTITY_MEMORY_FILE,
        "relationship_memory": RELATIONSHIP_MEMORY_FILE,
    }
