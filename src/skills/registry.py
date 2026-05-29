from src.skills.dependency_skill import DependencySkill
from src.skills.document_skill import DocumentSkill
from src.skills.entrypoint_skill import EntrypointSkill
from src.skills.memory_skill import MemorySkill
from src.skills.problem_skill import ProblemSkill


class SkillRegistry:
    def __init__(self):
        self.skills = {
            "memory": MemorySkill(),
            "problem": ProblemSkill(),
            "dependency": DependencySkill(),
            "entrypoint": EntrypointSkill(),
            "document": DocumentSkill(),
        }

    def get(self, name):
        return self.skills[name]
