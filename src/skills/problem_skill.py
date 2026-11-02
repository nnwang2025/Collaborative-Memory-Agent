from src.skills.utils import ROOT, bm25_rank, entity_score, extract_entities, format_skill_result, flatten_text, keyword_score, read_json_records


class ProblemSkill:
    """Search Bug, Issue, error logs and fix records from local mock files."""

    def __init__(self):
        self.paths = [
            ROOT / "data" / "issues",
            ROOT / "data" / "logs",
            ROOT / "data" / "commits",
        ]

    def _load_docs(self):
        docs = []
        for path in self.paths:
            if not path.exists():
                continue
            for file_path in sorted(path.glob("*.json")):
                for index, record in enumerate(read_json_records(file_path), start=1):
                    content = flatten_text(record)
                    docs.append({
                        "source": file_path.relative_to(ROOT).as_posix(),
                        "record": record.get("id") or record.get("issue_id") or record.get("commit") or index,
                        "content": content,
                        "metadata": record,
                    })
        return docs

    def run(self, question, top_k=5):
        docs = self._load_docs()
        hits = []
        for doc in docs:
            score = keyword_score(question, doc["content"]) + entity_score(question, doc["content"], doc["source"])
            if score > 0:
                hit = dict(doc)
                hit["score"] = float(score)
                hits.append(hit)
        bm25_hits = bm25_rank(question, docs)
        for hit in bm25_hits:
            hit["score"] = hit.get("score", 0) + entity_score(question, hit["content"], hit["source"])
        hits.extend(bm25_hits)

        unique = {}
        for hit in hits:
            key = (hit["source"], hit["record"])
            if key not in unique or hit["score"] > unique[key]["score"]:
                unique[key] = hit
        ranked = sorted(unique.values(), key=lambda item: item["score"], reverse=True)[:top_k]

        if not ranked:
            return format_skill_result("未在 Bug、Issue、日志或修复记录中检索到相关内容。", [], [], extract_entities(question))

        evidence = [dict(h) for h in ranked]
        sources = [h["source"] for h in ranked]
        entities = extract_entities(question + " " + " ".join(h["content"] for h in ranked))
        return format_skill_result("已从问题记录中找到相关 Bug/Issue/日志/修复线索。", evidence, sources, entities)
