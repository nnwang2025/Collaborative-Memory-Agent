from pathlib import Path

from src.skills.utils import (
    ROOT,
    bm25_rank,
    entity_score,
    extract_entities,
    flatten_text,
    format_skill_result,
    keyword_score,
    query_entity_hits,
    read_json_records,
    split_markdown,
)


class DocumentSkill:
    """Search local Markdown/Wiki/technical docs under docs/."""

    def __init__(self, docs_dir=None):
        self.docs_dir = Path(docs_dir) if docs_dir else ROOT / "docs"

    def _load_paragraphs(self):
        paragraphs = []
        if not self.docs_dir.exists():
            return paragraphs

        for path in sorted(self.docs_dir.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-8", errors="ignore")
            rel = path.relative_to(ROOT).as_posix()
            for index, paragraph in enumerate(split_markdown(text), start=1):
                paragraphs.append({
                    "source": rel,
                    "paragraph": index,
                    "content": paragraph,
                })
        return paragraphs

    def _load_entity_related_records(self, question):
        if not query_entity_hits(question):
            return []

        records = []
        json_sources = [
            ROOT / "data" / "decision_memory.json",
            ROOT / "data" / "issues",
            ROOT / "data" / "commits",
        ]
        for source in json_sources:
            files = sorted(source.glob("*.json")) if source.is_dir() else [source]
            for file_path in files:
                for index, record in enumerate(read_json_records(file_path), start=1):
                    content = flatten_text(record)
                    source_name = "decision_memory" if file_path.name == "decision_memory.json" else file_path.relative_to(ROOT).as_posix()
                    if entity_score(question, content, source_name) > 0:
                        records.append({
                            "source": source_name,
                            "paragraph": f"entity-{index}",
                            "content": content,
                        })
        return records

    def run(self, question, top_k=5):
        docs = self._load_paragraphs() + self._load_entity_related_records(question)
        keyword_hits = []
        for doc in docs:
            score = keyword_score(question, doc["content"]) + entity_score(question, doc["content"], doc["source"])
            if score > 0:
                hit = dict(doc)
                hit["score"] = float(score)
                keyword_hits.append(hit)

        bm25_hits = bm25_rank(question, docs)
        for hit in bm25_hits:
            hit["score"] = hit.get("score", 0) + entity_score(question, hit["content"], hit["source"])
        merged = {}
        for hit in keyword_hits + bm25_hits:
            key = (hit["source"], hit["paragraph"])
            current = merged.get(key)
            if not current or hit["score"] > current["score"]:
                merged[key] = hit

        hits = sorted(merged.values(), key=lambda item: item["score"], reverse=True)[:top_k]
        if not hits:
            return format_skill_result(
                "未在 docs/ Markdown 文档中检索到相关内容。",
                [],
                [],
                extract_entities(question),
            )

        evidence = [dict(hit) for hit in hits]
        sources = [hit["source"] for hit in hits]
        entities = extract_entities(question + " " + " ".join(hit["content"] for hit in hits))
        answer = f"在技术文档中找到 {len(hits)} 条相关内容，优先参考 {sources[0]}。"
        return format_skill_result(answer, evidence, sources, entities)
