from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ProjectConfig:
    name: str
    type: str
    path: str
    port: int | None = None


class ProjectLoader:
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.data = self._load_yaml()

    def _load_yaml(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise FileNotFoundError(f"Missing config: {self.config_path}")
        with self.config_path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def list_projects(self) -> list[ProjectConfig]:
        projects_section = self.data.get("projects", {})
        combined = []
        for group in ("frontend", "services"):
            for item in projects_section.get(group, []) or []:
                combined.append(
                    ProjectConfig(
                        name=item["name"],
                        type=item["type"],
                        path=item["path"],
                        port=item.get("port"),
                    )
                )
        return combined

    def get_project(self, project_name: str) -> ProjectConfig | None:
        return next((p for p in self.list_projects() if p.name == project_name), None)

    def resolve_owner(self, file_path: str) -> ProjectConfig | None:
        target = Path(file_path).resolve()
        for project in self.list_projects():
            root = Path(project.path).resolve()
            try:
                target.relative_to(root)
                return project
            except ValueError:
                continue
        return None
