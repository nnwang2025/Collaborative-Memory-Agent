from pathlib import Path

from src.skills.utils import ROOT, extract_entities, format_skill_result, keyword_score


class EntrypointSkill:
    """Locate project entrypoints and simple execution flow clues."""

    def __init__(self, project_root=None):
        self.project_root = Path(project_root) if project_root else ROOT

    def run(self, question, top_k=5):
        candidates = []
        for name in ("main.py", "app.py", "server.py", "cli.py"):
            path = self.project_root / name
            if path.exists():
                candidates.append(path)
        candidates.extend((self.project_root / "src").rglob("*.py") if (self.project_root / "src").exists() else [])

        hits = []
        entry_keywords = ["if __name__", "FastAPI", "uvicorn", "while", "input(", "app =", "CoordinatorAgent"]
        for path in sorted(set(candidates)):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if not any(keyword in text for keyword in entry_keywords):
                continue
            rel = path.relative_to(ROOT).as_posix()
            lines = []
            for number, line in enumerate(text.splitlines(), start=1):
                if any(keyword in line for keyword in entry_keywords) or keyword_score(question, line):
                    lines.append(f"L{number}: {line.strip()}")
            content = "\n".join(lines[:12])
            score = keyword_score(question, rel + "\n" + content) + 1
            hits.append({"source": rel, "content": content, "score": float(score)})

        ranked = sorted(hits, key=lambda item: item["score"], reverse=True)[:top_k]
        if not ranked:
            return format_skill_result("未定位到明确的项目入口或启动流程。", [], [], extract_entities(question))

        evidence = [{"source": h["source"], "content": h["content"], "score": h["score"]} for h in ranked]
        sources = [h["source"] for h in ranked]
        entities = extract_entities(question + " " + " ".join(h["content"] for h in ranked))
        return format_skill_result("已定位到项目入口和主要运行流程线索。", evidence, sources, entities)
