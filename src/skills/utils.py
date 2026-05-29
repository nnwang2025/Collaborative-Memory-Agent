import json
import math
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read_json_records(path):
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("records", "items", "data", "issues", "commits", "logs", "messages"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def flatten_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(flatten_text(v) for v in value.values())
    return str(value)


def tokenize(text):
    text = (text or "").lower()
    words = re.findall(r"[a-zA-Z0-9_./#-]+|[\u4e00-\u9fff]", text)
    return words


def split_markdown(text):
    parts = re.split(r"\n\s*\n", text or "")
    paragraphs = []
    for part in parts:
        paragraph = part.strip()
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def bm25_rank(query, docs):
    query_tokens = tokenize(query)
    if not query_tokens or not docs:
        return []

    tokenized_docs = [tokenize(doc["content"]) for doc in docs]
    doc_count = len(tokenized_docs)
    avgdl = sum(len(tokens) for tokens in tokenized_docs) / max(doc_count, 1)
    df = Counter()
    for tokens in tokenized_docs:
        df.update(set(tokens))

    k1 = 1.5
    b = 0.75
    ranked = []
    for doc, tokens in zip(docs, tokenized_docs):
        tf = Counter(tokens)
        score = 0.0
        doc_len = len(tokens) or 1
        for token in query_tokens:
            if token not in tf:
                continue
            idf = math.log(1 + (doc_count - df[token] + 0.5) / (df[token] + 0.5))
            denom = tf[token] + k1 * (1 - b + b * doc_len / max(avgdl, 1))
            score += idf * (tf[token] * (k1 + 1) / denom)
        if score > 0:
            item = dict(doc)
            item["score"] = round(score, 4)
            ranked.append(item)

    return sorted(ranked, key=lambda item: item["score"], reverse=True)


def keyword_score(query, text):
    query_lower = (query or "").lower()
    text_lower = (text or "").lower()
    score = 0
    if query_lower and query_lower in text_lower:
        score += 10
    for token in set(tokenize(query)):
        if token and token in text_lower:
            score += 1
    return score


def load_entity_memory():
    records = read_json_records(ROOT / "data" / "entity_memory.json")
    entities = []
    for record in records:
        name = record.get("entity", "")
        aliases = record.get("aliases", [])
        if name:
            entities.append(str(name))
        entities.extend(str(alias) for alias in aliases)
    return sorted(set(entities), key=len, reverse=True)


def query_entity_hits(query):
    query_lower = (query or "").lower()
    hits = []
    for entity in load_entity_memory():
        if entity.lower() in query_lower:
            hits.append(entity)
    return hits


def entity_score(query, text, source=""):
    hits = query_entity_hits(query)
    if not hits:
        return 0

    text_lower = (text or "").lower()
    score = 0
    for entity in hits:
        if entity.lower() in text_lower:
            score += 30

    source_lower = (source or "").lower()
    if score and (
        "decision_memory" in source_lower
        or "docs/" in source_lower
        or "issues/" in source_lower
        or "commits/" in source_lower
    ):
        score += 20
    return score


def extract_entities(text):
    entities = set()
    patterns = [
        r"\b[A-Z][A-Za-z0-9_]*(?:Service|Controller|Repository|Client|Manager|Agent|Skill)\b",
        r"\b[a-zA-Z_][a-zA-Z0-9_]*\.py\b",
        r"\bIssue\s*#?\d+\b",
        r"#\d+",
        r"\b[a-f0-9]{7,40}\b",
        r"\bFastAPI\b",
        r"\bLangGraph\b",
        r"\bRAG\b",
        r"\bBM25\b",
        r"\bEmbedding Retrieval\b",
        r"\bRerank\b",
        r"\bCoordinator Agent\b",
        r"\bCoordinatorAgent\b",
        r"\bMemorySkill\b",
        r"\bProblemSkill\b",
        r"\bDocumentSkill\b",
        r"\bDependencySkill\b",
        r"\bEntrypointSkill\b",
        r"\bExact Match\b",
        r"\bEntity Retrieval\b",
        r"\bSemantic Memory\b",
        r"\bEpisodic Memory\b",
        r"\bEntity Memory\b",
        r"\bRelationship Memory\b",
        r"\bDecision Memory\b",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text or "", flags=re.IGNORECASE):
            entities.add(match)
    for word in ["Agent架构", "混合检索", "长期记忆", "实体抽取", "多Skill路由", "检索优化", "Prompt设计", "Memory设计", "架构", "接口"]:
        if word in (text or ""):
            entities.add(word)
    return sorted(entities)


def format_skill_result(answer, evidence, sources, entities):
    return {
        "answer": answer,
        "evidence": evidence[:5],
        "sources": sorted(set(sources)),
        "entities": sorted(set(entities)),
    }
