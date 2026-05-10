"""Load skill markdown files for adapters that lack native skill
discovery (today: chat-only). SDK and CLI adapters pick up the same
files via their own ``.claude/skills/`` lookup."""

import os
import glob


class SkillsLoader:
    def __init__(self, skill_dirs: list[str] | str):
        # Accept a single string for backwards compatibility.
        if isinstance(skill_dirs, str):
            skill_dirs = [skill_dirs] if skill_dirs else []
        self.skill_dirs = [d for d in skill_dirs if d]
        self.skills: list[dict] = []
        self._load()

    def _load(self):
        self.skills = []
        seen_names: set[str] = set()
        # Earlier dirs win on name collision so per-agent skills
        # shadow daemon-wide ones.
        for skills_dir in self.skill_dirs:
            if not os.path.isdir(skills_dir):
                continue
            for path in sorted(glob.glob(os.path.join(skills_dir, "*.md"))):
                name = os.path.basename(path)
                if name == "README.md" or name in seen_names:
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                self.skills.append({"file": name, "content": content})
                seen_names.add(name)

    def get_context(self) -> str:
        if not self.skills:
            return ""
        parts = ["## Available Skills\n"]
        for skill in self.skills:
            parts.append(skill["content"])
        return "\n".join(parts)
