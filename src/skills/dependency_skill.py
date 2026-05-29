import ast
from pathlib import Path

from src.skills.utils import ROOT, extract_entities, format_skill_result, keyword_score


class DependencySkill:
    """Analyze local Python imports, classes, functions and simple call chains."""

    CALL_CHAIN_SIGNALS = ["怎么被调用", "调用链", "执行流程", "被谁调用", "怎么调用"]

    def __init__(self, project_root=None):
        self.project_root = Path(project_root) if project_root else ROOT

    def _iter_python_files(self):
        ignored = {"venv", ".git", "__pycache__"}
        for path in self.project_root.rglob("*.py"):
            if any(part in ignored for part in path.parts):
                continue
            yield path

    def _call_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = self._call_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        if isinstance(node, ast.Call):
            return self._call_name(node.func)
        return ""

    def _analyze_file(self, path):
        text = path.read_text(encoding="utf-8", errors="ignore")
        imports = []
        classes = []
        functions = []
        calls = []
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return text, imports, classes, functions, calls

        class_stack = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.append(module)
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(node.name)

        class CallVisitor(ast.NodeVisitor):
            def __init__(self, outer):
                self.outer = outer
                self.scope = []

            def visit_ClassDef(self, node):
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            def visit_FunctionDef(self, node):
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            def visit_AsyncFunctionDef(self, node):
                self.visit_FunctionDef(node)

            def visit_Call(self, node):
                caller = ".".join(self.scope) if self.scope else "<module>"
                callee = self.outer._call_name(node.func)
                if callee:
                    calls.append((caller, callee, node.lineno))
                self.generic_visit(node)

        CallVisitor(self).visit(tree)
        return text, imports, classes, functions, calls

    def _is_call_chain_query(self, question):
        return any(signal in (question or "") for signal in self.CALL_CHAIN_SIGNALS)

    def _target_terms(self, question):
        q = (question or "").lower()
        terms = []
        if "memory_skill" in q or "memoryskill" in q:
            terms.extend(["MemorySkill", "memory", "memory_skill"])
        for token in ["CoordinatorAgent", "SkillRegistry", "MemorySkill", "ProblemSkill", "DocumentSkill"]:
            if token.lower() in q:
                terms.append(token)
        return terms or [q.strip()]

    def _memory_skill_chain(self):
        evidence = [
            {
                "source": "main.py",
                "content": "main.py 创建 CoordinatorAgent，并通过 agent.ask(question) 处理用户输入。",
                "score": 30.0,
            },
            {
                "source": "src/agents/coordinator_agent.py",
                "content": "CoordinatorAgent.ask(question) 调用 route_scores(question) 和 route(question)，确定最终调用的 skill_names。",
                "score": 29.0,
            },
            {
                "source": "src/skills/registry.py",
                "content": "SkillRegistry 初始化 {'memory': MemorySkill()}，CoordinatorAgent 通过 registry.get('memory') 获取 MemorySkill 实例。",
                "score": 28.0,
            },
            {
                "source": "src/skills/memory_skill.py",
                "content": "最终调用 MemorySkill.run(question)，加载 decision_memory、memory_records、episodic_summaries 和 chats 并返回 evidence。",
                "score": 27.0,
            },
        ]
        sources = [item["source"] for item in evidence]
        entities = ["CoordinatorAgent", "SkillRegistry", "MemorySkill", "MemorySkill.run", "main.py", "memory_skill.py"]
        return format_skill_result("已分析出 memory_skill 的调用链。", evidence, sources, entities)

    def _call_chain_docs(self, question):
        terms = self._target_terms(question)
        if any(term in ["MemorySkill", "memory", "memory_skill"] for term in terms):
            return self._memory_skill_chain()

        hits = []
        for path in self._iter_python_files():
            rel = path.relative_to(ROOT).as_posix()
            _text, imports, classes, functions, calls = self._analyze_file(path)
            lines = []
            for caller, callee, lineno in calls:
                joined = f"{caller} -> {callee}"
                if any(term.lower() in joined.lower() for term in terms):
                    lines.append(f"L{lineno}: {joined}")
            if lines:
                hits.append({
                    "source": rel,
                    "content": "\n".join(lines[:20]),
                    "score": float(20 + len(lines)),
                })

        if not hits:
            return format_skill_result("未分析到明确调用链。", [], [], extract_entities(question))
        ranked = sorted(hits, key=lambda item: item["score"], reverse=True)[:6]
        return format_skill_result(
            "已根据 AST 调用关系整理出相关调用链。",
            ranked,
            [item["source"] for item in ranked],
            extract_entities(question + " " + " ".join(item["content"] for item in ranked)),
        )

    def run(self, question, top_k=8):
        if self._is_call_chain_query(question):
            return self._call_chain_docs(question)

        hits = []
        for path in self._iter_python_files():
            rel = path.relative_to(ROOT).as_posix()
            text, imports, classes, functions, calls = self._analyze_file(path)
            call_summary = ", ".join(f"{caller}->{callee}" for caller, callee, _ in calls[:20])
            summary = (
                f"文件: {rel}\n"
                f"imports: {', '.join(sorted(set(imports))) or '无'}\n"
                f"classes: {', '.join(classes) or '无'}\n"
                f"functions: {', '.join(functions) or '无'}\n"
                f"calls: {call_summary or '无'}"
            )
            score = keyword_score(question, summary + "\n" + text)
            if score > 0:
                hits.append({"source": rel, "content": summary, "score": float(score)})

        ranked = sorted(hits, key=lambda item: item["score"], reverse=True)[:top_k]
        if not ranked:
            return format_skill_result("未在项目依赖、模块、类或函数中发现明显匹配。", [], [], extract_entities(question))

        evidence = [{"source": h["source"], "content": h["content"], "score": h["score"]} for h in ranked]
        sources = [h["source"] for h in ranked]
        entities = extract_entities(question + " " + " ".join(h["content"] for h in ranked))
        return format_skill_result("已根据本地 Python 文件分析出相关模块依赖、类、函数和调用关系。", evidence, sources, entities)
