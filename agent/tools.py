from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain.tools import Tool

from digest.project_loader import ProjectLoader
from embeddings.embedder import Embedder
from embeddings.vector_store import VectorStore

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]
DIGESTS = ROOT / os.getenv("DIGESTS_PATH", "./digests")
LOADER = ProjectLoader(os.getenv("PROJECTS_CONFIG", str(ROOT / "projects.yaml")))


def _search(query: str, project: str | None = None) -> str:
    embedder = Embedder()
    store = VectorStore(os.getenv("CHROMA_PATH", "./vector_db"))
    q = embedder.embed_query(query)
    filters = {"project": project} if project else None
    results = store.query(q, n_results=8, filters=filters)
    return "\n".join(
        f"File: {r['metadata'].get('file_path', 'digest')}\nProject: {r['metadata'].get('project')}\n{r['content']}\n---"
        for r in results
    )


def search_codebase(query: str) -> str:
    return _search(query)


def search_by_project(raw: str) -> str:
    if "::" not in raw:
        return "Input must be '<project>::<query>'"
    project, query = raw.split("::", 1)
    return _search(query, project=project.strip())


def get_all_endpoints(service_name: str) -> str:
    p = DIGESTS / f"{service_name}.digest.json"
    if not p.exists():
        return f"Missing digest for {service_name}"
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = ["METHOD | PATH | AUTH | ROLES"]
    for ep in data.get("endpoints", []):
        rows.append(f"{ep['method']} | {ep['path']} | {ep['auth_required']} | {','.join(ep.get('roles', []))}")
    return "\n".join(rows)


def get_api_contracts(_: str = "") -> str:
    p = DIGESTS / "master.digest.json"
    if not p.exists():
        return "Missing master digest"
    data = json.loads(p.read_text(encoding="utf-8"))
    return "\n".join(
        f"{c['caller']} -> {c['service']} : {c['endpoint']} ({c.get('angular_service')})"
        for c in data.get("api_contracts", [])
    )


def get_service_dependencies(_: str = "") -> str:
    p = DIGESTS / "master.digest.json"
    if not p.exists():
        return "Missing master digest"
    data = json.loads(p.read_text(encoding="utf-8"))
    return "\n".join(f"{k} -> {', '.join(v)}" for k, v in data.get("service_dependencies", {}).items())


def get_entity_schema(entity_name: str) -> str:
    for file in DIGESTS.glob("*.digest.json"):
        data = json.loads(file.read_text(encoding="utf-8"))
        for ent in data.get("entities", []):
            if ent.get("name", "").lower() == entity_name.lower():
                return f"Entity={ent['name']} table={ent['table']} fields={ent['fields']} relationships={ent['relationships']}"
    return f"Entity not found: {entity_name}"


def read_source_file(file_path: str) -> str:
    p = Path(file_path).resolve()
    allowed_roots = [Path(pr.path).resolve() for pr in LOADER.list_projects()]
    if not any(str(p).startswith(str(root)) for root in allowed_roots):
        return "Access denied: file path is outside registered project directories."
    if not p.exists():
        return "File not found."
    content = p.read_text(encoding="utf-8", errors="ignore")
    return content[:4000]


def get_auth_flow(_: str = "") -> str:
    p = DIGESTS / "master.digest.json"
    if not p.exists():
        return "Missing master digest"
    data = json.loads(p.read_text(encoding="utf-8"))
    auth = data.get("auth_flow", {})
    return (
        f"type={auth.get('type')} issuer={auth.get('token_issuer')} "
        f"validated_by={auth.get('validated_by')} fe_interceptor={auth.get('fe_interceptor')}"
    )


def build_tools() -> list[Tool]:
    return [
        Tool(name="search_codebase", func=search_codebase, description="semantic search over indexed codebase"),
        Tool(name="search_by_project", func=search_by_project, description="project scoped search; input: project::query"),
        Tool(name="get_all_endpoints", func=get_all_endpoints, description="list endpoints for a service digest"),
        Tool(name="get_api_contracts", func=get_api_contracts, description="read frontend to backend API contracts"),
        Tool(name="get_service_dependencies", func=get_service_dependencies, description="service dependency tree"),
        Tool(name="get_entity_schema", func=get_entity_schema, description="entity fields and relations"),
        Tool(name="read_source_file", func=read_source_file, description="secure source file read inside registered projects"),
        Tool(name="get_auth_flow", func=get_auth_flow, description="JWT auth flow summary"),
    ]
