from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from langchain.tools import Tool

from digest.project_loader import ProjectLoader
from embeddings.embedder import Embedder
from embeddings.vector_store import VectorStore

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]
DIGESTS = ROOT / os.getenv("DIGESTS_PATH", "./digests")
GRAPH_FILE = ROOT / "graph" / "knowledge_graph.json"
LOADER = ProjectLoader(os.getenv("PROJECTS_CONFIG", str(ROOT / "projects.yaml")))

# Lazy-loaded graph store singleton
_graph_store = None


def _get_graph():
    global _graph_store
    if _graph_store is None:
        from graph.graph_store import GraphStore
        _graph_store = GraphStore(str(GRAPH_FILE))
    return _graph_store


def _reload_graph():
    global _graph_store
    _graph_store = None
    return _get_graph()


# ------------------------------------------------------------------
# Vector search tools
# ------------------------------------------------------------------

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


# ------------------------------------------------------------------
# Digest-based tools
# ------------------------------------------------------------------

def get_all_endpoints(service_name: str) -> str:
    p = DIGESTS / f"{service_name}.digest.json"
    if not p.exists():
        return f"Missing digest for {service_name}"
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = ["METHOD | PATH | AUTH | ROLES | HANDLER"]
    for ep in data.get("endpoints", []):
        rows.append(
            f"{ep['method']} | {ep['path']} | {ep.get('auth_required')} | "
            f"{','.join(ep.get('roles', []))} | {ep.get('handler', '')}"
        )
    # Also list beans
    beans = data.get("beans", [])
    if beans:
        rows.append(f"\nBeans ({len(beans)}):")
        for b in beans:
            rows.append(f"  [{b['bean_type']}] {b['name']} deps={b.get('dependencies', [])}")
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
                return (
                    f"Entity={ent['name']} table={ent['table']}\n"
                    f"fields={ent['fields']}\nrelationships={ent['relationships']}"
                )
    return f"Entity not found: {entity_name}"


def read_source_file(file_path: str) -> str:
    p = Path(file_path).resolve()
    allowed_roots = [Path(pr.path).resolve() for pr in LOADER.list_projects()]
    if not any(str(p).startswith(str(root)) for root in allowed_roots):
        return "Access denied: file path is outside registered project directories."
    if not p.exists():
        return "File not found."
    return p.read_text(encoding="utf-8", errors="ignore")[:4000]


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


# ------------------------------------------------------------------
# Deep search tools (Phase 4 improvements — Changes 1-3)
# ------------------------------------------------------------------

def _hit_key(hit: dict) -> str:
    md = hit.get("metadata", {})
    return (
        f"{md.get('project','')}::{md.get('file_path','')}::"
        f"{md.get('class_name','')}::{md.get('method_name','')}::"
        f"{hash(hit.get('content',''))}"
    )


def _rerank_hits(hits: list[dict], question: str) -> list[dict]:
    stop = {"what", "how", "does", "the", "and", "for", "with", "that", "this",
            "which", "when", "where", "from", "into", "about", "have", "been"}
    q_words = {w.lower() for w in re.findall(r'\b\w{4,}\b', question.lower())
               if w.lower() not in stop}

    def score(h: dict) -> float:
        md = h.get("metadata", {})
        searchable = (h.get("content", "") + " " + str(md)).lower()
        word_score = sum(1 for w in q_words if w in searchable)
        src_boost = {"digest": 1.5, "graph": 1.2, "code": 0.0}.get(md.get("source", "code"), 0.0)
        type_boost = {
            "method_call_graph": 1.0, "endpoint": 0.8, "bean": 0.6,
            "entity": 0.5, "feign": 0.5,
        }.get(md.get("type", ""), 0.0)
        dist_score = max(0.0, 1.0 - h.get("distance", 1.0))
        return word_score + src_boost + type_boost + dist_score

    return sorted(hits, key=score, reverse=True)


def search_deep(query: str) -> str:
    """
    Multi-hop, re-ranked semantic search for deep questions.
    Runs 8 targeted query variants, follows class name references one hop,
    re-ranks by question relevance. Returns up to 30 chunks.
    """
    embedder = Embedder()
    store = VectorStore(os.getenv("CHROMA_PATH", "./vector_db"))

    # Build targeted variants (Change 3)
    variants = [
        query,
        f"service method implementation business logic: {query}",
        f"REST endpoint controller handler DTO: {query}",
        f"entity repository database query: {query}",
        f"configuration properties Feign Kafka event: {query}",
        f"Angular component service HTTP call: {query}",
        f"method call graph dependencies: {query}",
    ]
    svc = re.search(r'ms-java-[\w-]+|module-java-[\w-]+', query)
    if svc:
        variants.append(f"{svc.group(0)}: {query}")
    cls = re.search(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', query)
    if cls:
        variants.append(f"{cls.group(1)} method calls dependencies implementation")

    merged: dict[str, dict] = {}
    for v in variants[:8]:
        qvec = embedder.embed_query(v)
        for hit in store.query(qvec, n_results=12):
            key = _hit_key(hit)
            if key not in merged or hit.get("distance", 9e9) < merged[key].get("distance", 9e9):
                merged[key] = hit

    # Multi-hop: follow class names found in initial results (Change 1)
    found_names: set[str] = set()
    for hit in merged.values():
        found_names.update(re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', hit.get("content", "")))
    skip_common = {"Optional", "ResponseEntity", "HttpStatus", "List", "Map",
                   "String", "Long", "Integer", "Boolean", "Object"}
    for name in [n for n in found_names if n not in skip_common][:8]:
        qvec = embedder.embed_query(f"{name} implementation method calls")
        for hit in store.query(qvec, n_results=4):
            key = _hit_key(hit)
            if key not in merged:
                merged[key] = hit

    # Re-rank and cap (Change 2)
    ranked = _rerank_hits(list(merged.values()), query)[:30]

    return "\n\n".join(
        "[{i}] project={p} type={t} file={f} class={c} method={m}\n{content}".format(
            i=i + 1,
            p=h["metadata"].get("project", ""),
            t=h["metadata"].get("type", ""),
            f=h["metadata"].get("file_path", "digest"),
            c=h["metadata"].get("class_name", ""),
            m=h["metadata"].get("method_name", ""),
            content=h.get("content", ""),
        )
        for i, h in enumerate(ranked)
    )


def get_method_calls(class_name: str) -> str:
    """
    Look up the method call graph for a class from the digest.
    Input: "ClassName" or "service-name::ClassName"
    Shows which injected dependency methods each service method calls.
    """
    project_filter: str | None = None
    if "::" in class_name:
        project_filter, class_name = class_name.split("::", 1)
    class_name = class_name.strip()

    for f in DIGESTS.glob("*.digest.json"):
        if f.name == "master.digest.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if project_filter and data.get("project") != project_filter:
            continue

        for bean in data.get("beans", []):
            if bean.get("name", "").lower() == class_name.lower():
                project = data.get("project", "unknown")
                method_calls = bean.get("method_calls", {})
                queries = bean.get("queries", [])

                if not method_calls and not queries:
                    return (
                        f"{class_name} found in {project} but no method call graph available. "
                        f"Run /reindex to rebuild — method call extraction requires a fresh digest."
                    )
                lines = [f"Method call graph for {class_name} [{project}]:"]
                for method, calls in method_calls.items():
                    lines.append(f"  {method}() calls:")
                    for call in calls:
                        lines.append(f"    → {call}")
                if queries:
                    lines.append(f"\n@Query methods ({len(queries)}):")
                    for q in queries[:5]:
                        lines.append(f"  {q[:200]}")
                return "\n".join(lines)

    return (
        f"Class '{class_name}' not found as a @Service/@Repository bean in any digest. "
        f"It may be a controller, DTO, or not yet indexed — run /reindex."
    )


# ------------------------------------------------------------------
# Graph traversal tools  (Phase 2)
# ------------------------------------------------------------------

def trace_request(query: str) -> str:
    """
    Trace a request end-to-end through the knowledge graph.
    Input: endpoint path (e.g. "/api/orders") or "GET /api/orders"
    Returns: Angular callers → controller → service → repository → entity, including Feign callers.
    """
    return _get_graph().trace_request(query)


def find_callers(query: str) -> str:
    """
    Find all callers of an endpoint, service bean, or Angular service.
    Input: class name ("OrderService"), path ("/api/orders"), or Kafka topic name.
    Returns: all inbound edges to the matched node.
    """
    return _get_graph().find_callers(query)


def impact_graph(query: str) -> str:
    """
    BFS impact analysis: find all code artifacts affected by a change to this node.
    Input: class name, entity name, endpoint path, or Kafka topic.
    Returns: layered impact report with risk level.
    """
    return _get_graph().impact_graph(query)


def graph_summary(_: str = "") -> str:
    """Return a summary of the knowledge graph (node/edge counts by type)."""
    return _get_graph().summary()


# ------------------------------------------------------------------
# Tool registry
# ------------------------------------------------------------------

def build_tools_map() -> dict:
    """Plain dict of tool_name → callable — used by AgentLoop (no langchain dependency)."""
    return {
        "search_codebase": search_codebase,
        "search_by_project": search_by_project,
        "search_deep": search_deep,
        "get_method_calls": get_method_calls,
        "get_all_endpoints": get_all_endpoints,
        "get_api_contracts": get_api_contracts,
        "get_service_dependencies": get_service_dependencies,
        "get_entity_schema": get_entity_schema,
        "read_source_file": read_source_file,
        "get_auth_flow": get_auth_flow,
        "trace_request": trace_request,
        "find_callers": find_callers,
        "impact_graph": impact_graph,
        "graph_summary": graph_summary,
    }


def build_tools() -> list[Tool]:
    return [
        # Vector search
        Tool(name="search_codebase", func=search_codebase,
             description="Semantic vector search over the indexed codebase. Use for finding code by concept."),
        Tool(name="search_by_project", func=search_by_project,
             description="Project-scoped semantic search. Input format: '<project>::<query>'"),
        Tool(name="search_deep", func=search_deep,
             description=(
                 "Multi-hop re-ranked deep search. Runs 8 targeted query variants, follows class name "
                 "references one hop, and re-ranks by question relevance. Use this instead of "
                 "search_codebase for deep/architecture/flow questions."
             )),
        Tool(name="get_method_calls", func=get_method_calls,
             description=(
                 "Look up the method call graph for a @Service or @Repository class from the digest. "
                 "Shows which injected dependency methods each service method calls. "
                 "Input: 'ClassName' or 'service-name::ClassName'."
             )),
        # Digest tools
        Tool(name="get_all_endpoints", func=get_all_endpoints,
             description="List all REST endpoints and beans for a service. Input: service name."),
        Tool(name="get_api_contracts", func=get_api_contracts,
             description="List all Angular-to-backend API contracts (which FE service calls which BE endpoint)."),
        Tool(name="get_service_dependencies", func=get_service_dependencies,
             description="Show the inter-service dependency tree (Feign + Kafka edges)."),
        Tool(name="get_entity_schema", func=get_entity_schema,
             description="Get JPA entity fields and relationships. Input: entity class name."),
        Tool(name="read_source_file", func=read_source_file,
             description="Read actual source file content. Input: absolute file path inside a registered project."),
        Tool(name="get_auth_flow", func=get_auth_flow,
             description="Describe the JWT auth flow: who issues tokens, who validates, which FE interceptor adds headers."),
        # Graph tools
        Tool(name="trace_request", func=trace_request,
             description=(
                 "Trace a request end-to-end through the knowledge graph. "
                 "Input: endpoint path like '/api/orders' or 'GET /api/orders'. "
                 "Returns: Angular callers → controller → service → repo → entity chain."
             )),
        Tool(name="find_callers", func=find_callers,
             description=(
                 "Find everything that calls a given endpoint, bean, or service. "
                 "Input: class name ('OrderService'), path ('/api/orders'), or topic name."
             )),
        Tool(name="impact_graph", func=impact_graph,
             description=(
                 "BFS impact analysis from a class, entity, or endpoint. "
                 "Input: name of the artifact to analyze. "
                 "Returns: all impacted artifacts across all services with risk level."
             )),
        Tool(name="graph_summary", func=graph_summary,
             description="Show knowledge graph statistics: node/edge counts by type."),
    ]
