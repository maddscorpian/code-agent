from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from .angular_parser import AngularParser
from .master_digest_builder import MasterDigestBuilder
from .project_loader import ProjectLoader
from .springboot_parser import SpringBootParser

load_dotenv()
logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_data_root_env = os.getenv("AGENT_DATA_ROOT", "")
_DATA_ROOT = Path(_data_root_env).resolve() if _data_root_env else _REPO_ROOT

GRAPH_DIR = Path(os.getenv("GRAPH_PATH", str(_DATA_ROOT / "graph" / "knowledge_graph.json"))).parent
GRAPH_FILE = GRAPH_DIR / "knowledge_graph.json"


class DigestRunner:
    def __init__(self, config_path: str):
        self.loader = ProjectLoader(config_path)
        _digests_env = os.getenv("DIGESTS_PATH", "")
        self.output_dir = (
            Path(_digests_env).resolve() if _digests_env and Path(_digests_env).is_absolute()
            else _DATA_ROOT / (os.getenv("DIGESTS_PATH", "digests"))
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        _catalog_env = os.getenv("API_CATALOG_PATH", "")
        self._catalog_dir = (
            Path(_catalog_env).resolve() if _catalog_env and Path(_catalog_env).is_absolute()
            else _DATA_ROOT / (os.getenv("API_CATALOG_PATH", "api-catalog"))
        )

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
        self._build_graph()
        print(
            f"projects parsed={stats['projects']} endpoints={stats['endpoints']} entities={stats['entities']}"
        )

    def run_single(self, project_name: str):
        self._run_project(project_name)
        self._rebuild_master_from_disk()
        self._build_graph()

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
        normalized_type = self._normalize_project_type(p.type)
        if normalized_type == "spring-boot":
            digest = SpringBootParser(p.path).parse()
        elif normalized_type == "angular":
            digest = AngularParser(p.path).parse()
        else:
            raise ValueError(f"Unsupported type: {p.type}. Supported: angular, spring-boot (aliases: maven, gradle, spring, springboot)")
        self._write_json(self.output_dir / f"{p.name}.digest.json", digest.model_dump())
        return digest

    @staticmethod
    def _normalize_project_type(project_type: str) -> str:
        t = (project_type or "").strip().lower()
        if t in {"spring-boot", "springboot", "spring", "maven", "gradle"}:
            return "spring-boot"
        return t

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

    def _build_graph(self) -> None:
        try:
            from graph.graph_builder import GraphBuilder
            GRAPH_DIR.mkdir(parents=True, exist_ok=True)
            graph = GraphBuilder(str(self.output_dir)).build()
            self._write_json(GRAPH_FILE, graph)
            logger.info(
                "Knowledge graph built: %d nodes, %d edges",
                graph["stats"]["nodes"],
                graph["stats"]["edges"],
            )
        except Exception as exc:
            logger.warning("Graph build failed (non-fatal): %s", exc)
        self._build_api_catalog()

    def _build_api_catalog(self) -> None:
        try:
            from digest.api_catalog_builder import ApiCatalogBuilder
            self._catalog_dir.mkdir(parents=True, exist_ok=True)
            catalog = ApiCatalogBuilder(str(self.output_dir), str(self._catalog_dir)).build()
            total = catalog.get("info", {}).get("x-total-endpoints", "?")
            logger.info("API catalog built: %s endpoints → %s", total, self._catalog_dir)
        except Exception as exc:
            logger.warning("API catalog build failed (non-fatal): %s", exc)

    @staticmethod
    def _write_json(path: Path, payload: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO)
    config = os.getenv("PROJECTS_CONFIG", "./projects.yaml")
    DigestRunner(config).run_all()
