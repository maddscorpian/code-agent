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
    # Retrieve 16 candidates then rerank to best 8 — improves chat-mode relevance
    results = store.query(q, n_results=16, filters=filters)
    results = _rerank_hits(results, query)[:8]
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

    variants = [
        query,
        f"service method implementation business logic: {query}",
        f"REST endpoint controller handler DTO: {query}",
        f"entity repository database query: {query}",
        f"configuration properties Feign Kafka event: {query}",
        f"Angular component service HTTP call: {query}",
        f"method call graph dependencies: {query}",
        f"security authentication authorization @PreAuthorize JWT role: {query}",
        f"exception error handling @ControllerAdvice fault tolerance: {query}",
        f"Kafka event topic property configuration value: {query}",
    ]
    svc = re.search(r'ms-java-[\w-]+|module-java-[\w-]+', query)
    if svc:
        variants.append(f"{svc.group(0)}: {query}")
    cls = re.search(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', query)
    if cls:
        variants.append(f"{cls.group(1)} method calls dependencies implementation")

    merged: dict[str, dict] = {}
    for v in variants[:10]:
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


def _extract_method_body(source: str, method_name: str) -> str:
    """
    Locate a method by name in Java source and return its body (brace-depth scan).
    Returns up to 3000 chars. Returns "" if not found.
    """
    sig_re = re.compile(
        rf'(?:(?:public|private|protected|static|final|synchronized|override)\s+)*'
        rf'[\w<>\[\],\s]+\s+{re.escape(method_name)}\s*\(',
    )
    m = sig_re.search(source)
    if not m:
        return ""
    brace_start = source.find('{', m.start())
    if brace_start == -1:
        return ""
    depth = 0
    for i in range(brace_start, min(brace_start + 4000, len(source))):
        if source[i] == '{':
            depth += 1
        elif source[i] == '}':
            depth -= 1
            if depth == 0:
                return source[brace_start: i + 1].strip()[:3000]
    return source[brace_start: brace_start + 3000].strip()


def get_method_implementation(query: str) -> str:
    """
    Return the actual source code of a service or repository method.
    Input: "ClassName::methodName" or "ClassName.methodName" or just "ClassName"
    Locates the file via the digest, reads it, and extracts the specific method body.
    """
    # Parse class and optional method from input
    method_name: str | None = None
    if "::" in query:
        class_name, method_name = query.split("::", 1)
    elif "." in query and query[query.rfind(".") + 1].islower():
        class_name, method_name = query.rsplit(".", 1)
    else:
        class_name = query
    class_name = class_name.strip()
    if method_name:
        method_name = method_name.strip().rstrip("()")

    # Find class in digests
    for f in DIGESTS.glob("*.digest.json"):
        if f.name == "master.digest.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for bean in data.get("beans", []):
            if bean.get("name", "").lower() != class_name.lower():
                continue
            project = data.get("project", "")
            file_rel = bean.get("file_path", "")
            if not file_rel:
                return f"{class_name} found in {project} but file_path missing from digest."
            # Resolve absolute path via project loader
            proj_obj = LOADER.get_project(project)
            if not proj_obj:
                return f"{class_name} found in project '{project}' but project not in projects.yaml."
            abs_path = Path(proj_obj.path) / file_rel
            if not abs_path.exists():
                return f"Source file not found at {abs_path}"
            source = abs_path.read_text(encoding="utf-8", errors="ignore")

            if not method_name:
                # No method specified — return class overview (first 3000 chars)
                return f"Source of {class_name} [{project}] ({file_rel}):\n{source[:3000]}"

            body = _extract_method_body(source, method_name)
            if body:
                return (
                    f"Implementation of {class_name}.{method_name}() "
                    f"[{project}] ({file_rel}):\n\n{body}"
                )
            # Method not found in source — check method_bodies from digest
            mb = bean.get("method_bodies", {})
            if method_name in mb:
                return (
                    f"Implementation of {class_name}.{method_name}() "
                    f"[{project}] (from digest excerpt):\n\n{mb[method_name]}"
                )
            # List available methods as fallback
            available = bean.get("methods", [])
            return (
                f"Method '{method_name}' not found in {class_name} [{project}].\n"
                f"Available methods: {', '.join(available[:20])}"
            )

    return (
        f"Class '{class_name}' not found in any digest. "
        f"It may not be a @Service/@Repository, or not yet indexed — run /reindex."
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


def list_features(project_filter: str = "") -> str:
    """
    List all detected user functions/features in the system.
    Input: project name to filter (e.g. 'pan-portal') or empty string for all.
    """
    return _get_graph().list_features(project_filter)


def describe_feature(feature_name: str) -> str:
    """
    Full end-to-end trace for a named user function.
    Input: feature name (e.g. 'Book Appointment', 'cancel order', 'move-site').
    Fuzzy-matched. Returns Angular components → services → Spring beans → repos.
    """
    return _get_graph().describe_feature(feature_name)


def trace_event_flow(topic_or_service: str) -> str:
    """
    Trace the complete Kafka event flow for a topic or service.
    Input: topic name (e.g. 'order.events') or service name (e.g. 'ms-java-order').
    Returns: REST publisher endpoint(s) → Kafka topic → all consumer services and handler beans.
    """
    return _get_graph().trace_event_flow(topic_or_service)


def get_dto_schema(dto_name: str) -> str:
    """
    Return the field structure of a request/response DTO class.
    Input: DTO class name (e.g. 'OrderRequest', 'AppointmentSlotResponse').
    Shows field names, types, required status, @JsonProperty, and validation annotations.
    """
    matches: list[str] = []
    dto_lower = dto_name.lower().strip()

    for f in DIGESTS.glob("*.digest.json"):
        if f.name == "master.digest.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        project = data.get("project", "")
        for dto in data.get("dto_schemas", []):
            name = dto.get("name", "")
            if dto_lower not in name.lower():
                continue
            lines = [f"DTO: {name} [{project}]  file={dto.get('file_path', '')}"]
            lines.append(f"{'Field':<30} {'Type':<30} {'Req':>3}  {'Validations'}")
            lines.append("-" * 80)
            for fld in dto.get("fields", []):
                req = "✓" if fld.get("required") else ""
                jp = f"  @JsonProperty({fld['json_property']!r})" if fld.get("json_property") else ""
                vals = ", ".join(fld.get("validations", []))
                lines.append(
                    f"  {fld['name']:<28} {fld['type']:<30} {req:>3}  {vals}{jp}"
                )
            # Also show Feign calls that use this DTO
            feign_uses: list[str] = []
            for fc in data.get("feign_clients", []):
                for cd in fc.get("call_details", []):
                    if dto_lower in (cd.get("request_dto","").lower(), cd.get("response_dto","").lower()):
                        feign_uses.append(f"{fc['client_name']}: {cd['method']} {cd['path']}")
            if feign_uses:
                lines.append(f"\nUsed in Feign calls:")
                for u in feign_uses:
                    lines.append(f"  {u}")
            matches.append("\n".join(lines))

    if not matches:
        return f"DTO '{dto_name}' not found in any digest. Run /reindex to rebuild."
    return "\n\n".join(matches)


def get_external_calls(service_filter: str = "") -> str:
    """
    Return all Feign client downstream calls with full request/response details.
    Shows resolved URLs, HTTP methods, paths, request DTOs (with fields), response DTOs,
    path params, query params, and OAuth scopes.
    Marks calls as [internal] (to another indexed microservice) or [external] (third-party API).
    Input: service name (e.g. 'ms-java-order') or empty string for all services.
    """
    # Build DTO schema lookup across all digests: {type_name: [field descriptions]}
    dto_schemas: dict[str, list[str]] = {}
    for df in DIGESTS.glob("*.digest.json"):
        if df.name == "master.digest.json":
            continue
        try:
            ddata = json.loads(df.read_text(encoding="utf-8"))
        except Exception:
            continue
        for dto in ddata.get("dto_schemas", []):
            name = dto.get("name", "")
            if name and name not in dto_schemas:
                fields = [
                    f"{fld['name']}: {fld['type']}"
                    + (" [required]" if fld.get("required") else "")
                    + (f" @JsonProperty({fld['json_property']!r})" if fld.get("json_property") else "")
                    for fld in dto.get("fields", [])[:8]
                ]
                if fields:
                    dto_schemas[name] = fields

    # Collect all known internal project names for internal/external classification
    known_projects: set[str] = set()
    for df in DIGESTS.glob("*.digest.json"):
        if df.name == "master.digest.json":
            continue
        try:
            ddata = json.loads(df.read_text(encoding="utf-8"))
            p = ddata.get("project", "")
            if p:
                known_projects.add(p.lower())
        except Exception:
            continue

    def _is_internal(target: str) -> bool:
        tl = target.lower()
        return any(tl in p or p in tl for p in known_projects)

    def _dto_fields(type_name: str) -> list[str]:
        if not type_name:
            return []
        return dto_schemas.get(type_name, [])

    results: list[str] = []
    for f in DIGESTS.glob("*.digest.json"):
        if f.name == "master.digest.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        project = data.get("project", "")
        if service_filter and service_filter.lower() not in project.lower():
            continue
        feigns = data.get("feign_clients", [])
        if not feigns:
            continue

        lines = [f"[{project}] Downstream Feign clients:"]
        for fc in feigns:
            target = fc.get("target_service", "unknown")
            tag = "[internal]" if _is_internal(target) else "[external]"
            url = fc.get("resolved_url", "") or f"${{{fc.get('url_property_key', 'unresolved')}}}"
            scope = fc.get("oauth_scope", "")
            scope_str = f"  oauth_scope={scope}" if scope else ""
            lines.append(f"")
            lines.append(f"  {fc['client_name']} {tag}")
            lines.append(f"    target : {target}")
            lines.append(f"    baseUrl : {url}{scope_str}")

            # Show each method with full request/response detail
            for cd in fc.get("call_details", []):
                method = cd.get("method", "GET")
                path = cd.get("path", "")
                req_dto = cd.get("request_dto", "")
                resp_dto = cd.get("response_dto", "")
                path_params = cd.get("path_params", [])
                req_params = cd.get("request_params", [])

                lines.append(f"")
                lines.append(f"    {method} {path}")
                if path_params:
                    lines.append(f"      pathParams  : {', '.join(path_params)}")
                if req_params:
                    lines.append(f"      queryParams : {', '.join(req_params)}")
                if req_dto:
                    lines.append(f"      request     : {req_dto}")
                    fields = _dto_fields(req_dto)
                    for fld in fields:
                        lines.append(f"        - {fld}")
                if resp_dto:
                    lines.append(f"      response    : {resp_dto}")
                    fields = _dto_fields(resp_dto)
                    for fld in fields:
                        lines.append(f"        - {fld}")

            # Fallback to simple call strings if no call_details
            if not fc.get("call_details"):
                for call in fc.get("calls", [])[:8]:
                    lines.append(f"    {call}")

        results.append("\n".join(lines))

    return "\n\n".join(results) if results else (
        f"No Feign clients found for '{service_filter}'. "
        "Run /reindex or check that the service name matches a project in projects.yaml."
    )


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
        "get_method_implementation": get_method_implementation,
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
        "list_features": list_features,
        "describe_feature": describe_feature,
        "get_external_calls": get_external_calls,
        "get_dto_schema": get_dto_schema,
        "trace_event_flow": trace_event_flow,
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
        Tool(name="get_method_implementation", func=get_method_implementation,
             description=(
                 "Return the actual Java source code of a specific service or repository method. "
                 "Reads the source file directly — shows real business logic, not just call graphs. "
                 "Input: 'ClassName::methodName' or 'ClassName.methodName' or just 'ClassName'."
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
        Tool(name="list_features", func=list_features,
             description=(
                 "List all detected user functions (features) in the system. "
                 "Input: project name to filter (e.g. 'pan-portal') or empty string for all. "
                 "Use when asked 'what can a user do?' or 'what features exist?'"
             )),
        Tool(name="describe_feature", func=describe_feature,
             description=(
                 "Full end-to-end trace for a named user function/feature. "
                 "Input: feature name (e.g. 'Book Appointment', 'cancel order'). Fuzzy-matched. "
                 "Returns: Angular components → Angular services → Spring beans → repositories. "
                 "Use for 'how does [feature] work?' questions."
             )),
        Tool(name="get_external_calls", func=get_external_calls,
             description=(
                 "List all Feign client downstream calls for a service with resolved URLs. "
                 "Input: service name (e.g. 'ms-java-order') or empty string for all. "
                 "Use when asked 'what does X call?' or 'what downstream services does X depend on?'"
             )),
        Tool(name="get_dto_schema", func=get_dto_schema,
             description=(
                 "Return the complete field structure of a request or response DTO class. "
                 "Input: DTO class name (e.g. 'OrderRequest', 'AppointmentSlotResponse'). "
                 "Use when asked 'what fields does X have?' or 'what does the API request/response look like?'"
             )),
        Tool(name="trace_event_flow", func=trace_event_flow,
             description=(
                 "Trace the complete Kafka event flow for a topic or service. "
                 "Input: topic name (e.g. 'order.events') or service name. "
                 "Returns: REST publisher endpoint → Kafka topic → consumer services and handlers. "
                 "Use for 'how does event X flow?', 'who consumes order events?', 'what events does X produce?'"
             )),
    ]
