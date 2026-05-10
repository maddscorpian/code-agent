from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .angular_parser import AngularParser
from .master_digest_builder import MasterDigestBuilder
from .project_loader import ProjectLoader
from .springboot_parser import SpringBootParser

logger = logging.getLogger(__name__)


class DigestRunner:
    def __init__(self, config_path: str):
        self.loader = ProjectLoader(config_path)
        self.output_dir = Path(__file__).resolve().parents[1] / "digests"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run_all(self):
        service_digests = []
        angular_digest = None
        stats = {"projects": 0, "endpoints": 0, "entities": 0}

        for project in self.loader.list_projects():
            start = time.perf_counter()
            try:
                digest = self._run_project(project.name)
                if not digest:
                    continue
                stats["projects"] += 1
                if digest.type == "spring-boot":
                    service_digests.append(digest)
                    stats["endpoints"] += len(digest.endpoints)
                    stats["entities"] += len(digest.entities)
                else:
                    angular_digest = digest
                logger.info("Parsed %s in %.2fs", project.name, time.perf_counter() - start)
            except Exception as exc:
                logger.exception("Failed parsing %s: %s", project.name, exc)

        self._write_master(service_digests, angular_digest)
        print(
            f"projects parsed={stats['projects']} endpoints={stats['endpoints']} entities={stats['entities']}"
        )

    def run_single(self, project_name: str):
        self._run_project(project_name)
        self._rebuild_master_from_disk()

    def run_incremental(self, changed_file: str):
        owner = self.loader.resolve_owner(changed_file)
        if not owner:
            logger.warning("No owning project found for %s", changed_file)
            return
        self.run_single(owner.name)

    def _run_project(self, project_name: str):
        p = self.loader.get_project(project_name)
        if not p:
            raise ValueError(f"Unknown project: {project_name}")
        if p.type == "spring-boot":
            digest = SpringBootParser(p.path).parse()
        elif p.type == "angular":
            digest = AngularParser(p.path).parse()
        else:
            raise ValueError(f"Unsupported type: {p.type}")
        self._write_json(self.output_dir / f"{p.name}.digest.json", digest.model_dump())
        return digest

    def _rebuild_master_from_disk(self):
        service_digests = []
        angular_digest = None
        from .models import AngularDigest, ServiceDigest

        for p in self.loader.list_projects():
            path = self.output_dir / f"{p.name}.digest.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if p.type == "spring-boot":
                service_digests.append(ServiceDigest(**data))
            elif p.type == "angular":
                angular_digest = AngularDigest(**data)
        self._write_master(service_digests, angular_digest)

    def _write_master(self, service_digests, angular_digest):
        master = MasterDigestBuilder(service_digests, angular_digest).build()
        self._write_json(self.output_dir / "master.digest.json", master.model_dump())

    @staticmethod
    def _write_json(path: Path, payload: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO)
    config = os.getenv("PROJECTS_CONFIG", "./projects.yaml")
    DigestRunner(config).run_all()
