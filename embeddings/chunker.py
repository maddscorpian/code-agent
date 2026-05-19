from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()   # ensure TIKTOKEN_CACHE_DIR (and other vars) are in os.environ
                # before tiktoken tries to resolve its cache path

logger = logging.getLogger(__name__)


class Chunker:
    def __init__(self, root: str):
        self.root = Path(root)
        self.digests_dir = self.root / "digests"
        self.encoder = None
        try:
            import tiktoken
            self.encoder = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:
            logger.warning("tiktoken unavailable; using char-based chunking fallback: %s", exc)

    def build_chunks(self) -> list[dict]:
        chunks: list[dict] = []
        chunks.extend(self._digest_chunks())
        chunks.extend(self._code_chunks())
        chunks.extend(self._graph_chunks())
        chunks.extend(self._feature_chunks())
        return chunks

    # ------------------------------------------------------------------
    # Digest-derived chunks
    # ------------------------------------------------------------------

    def _digest_chunks(self) -> list[dict]:
        rows: list[dict] = []
        for file in self.digests_dir.glob("*.digest.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
            except Exception:
                continue
            project = data.get("project") or data.get("system", "master")

            # Endpoints
            for i, ep in enumerate(data.get("endpoints", [])):
                content = (
                    f"Endpoint {ep.get('method')} {ep.get('path')}\n"
                    f"controller={ep.get('controller')} handler={ep.get('handler')}\n"
                    f"request={ep.get('request_dto')} response={ep.get('response_dto')}\n"
                    f"auth={ep.get('auth_required')} roles={ep.get('roles')}"
                )
                if ep.get("javadoc"):
                    content += f"\ndoc={ep['javadoc']}"
                rows.append(self._chunk_dict(project, str(file), i, content, {
                    "source": "digest", "project": project, "type": "endpoint",
                    "name": ep.get("handler", "endpoint"),
                }))

            # Entities
            for i, ent in enumerate(data.get("entities", [])):
                content = (
                    f"Entity {ent.get('name')} table={ent.get('table')}\n"
                    f"fields={ent.get('fields')}\nrelationships={ent.get('relationships')}"
                )
                rows.append(self._chunk_dict(project, str(file), 1000 + i, content, {
                    "source": "digest", "project": project, "type": "entity",
                    "name": ent.get("name", "entity"),
                }))

            # Feign clients
            for i, fc in enumerate(data.get("feign_clients", [])):
                content = f"Feign {fc.get('client_name')} target={fc.get('target_service')}"
                if fc.get("resolved_url"):
                    content += f"\nresolved_url={fc['resolved_url']}"
                elif fc.get("url_property_key"):
                    content += f"\nurl_property_key={fc['url_property_key']} (check application.properties)"
                if fc.get("oauth_scope"):
                    content += f"\noauth_scope={fc['oauth_scope']}"
                content += f"\ncalls={fc.get('calls')}"
                rows.append(self._chunk_dict(project, str(file), 2000 + i, content, {
                    "source": "digest", "project": project, "type": "feign",
                    "name": fc.get("client_name", "feign"),
                }))

            # Spring beans (services, repositories, components)
            for i, bean in enumerate(data.get("beans", [])):
                content = (
                    f"Bean {bean.get('name')} type={bean.get('bean_type')} file={bean.get('file_path')}\n"
                    f"dependencies={bean.get('dependencies')}\n"
                    f"methods={bean.get('methods')}\n"
                    f"transactional_methods={bean.get('transactional_methods')}"
                )
                # Include method call graph so the LLM can trace internal logic (Change 4)
                method_calls = bean.get("method_calls", {})
                if method_calls:
                    call_lines = [f"  {m}() → {', '.join(calls)}" for m, calls in list(method_calls.items())[:10]]
                    content += "\nmethod_call_graph:\n" + "\n".join(call_lines)
                # Include JPQL queries for repositories (Change 5)
                queries = bean.get("queries", [])
                if queries:
                    content += "\nqueries:\n" + "\n".join(f"  {q[:200]}" for q in queries[:5])
                rows.append(self._chunk_dict(project, str(file), 3000 + i, content, {
                    "source": "digest", "project": project, "type": "bean",
                    "name": bean.get("name", "bean"), "class_name": bean.get("name"),
                }))
                # Separate method-call-graph chunk for targeted retrieval
                if method_calls:
                    mcg_content = f"Method call graph for {bean.get('name')} [{project}]:\n"
                    mcg_content += "\n".join(
                        f"  {m}() calls: {', '.join(calls)}"
                        for m, calls in method_calls.items()
                    )
                    rows.append(self._chunk_dict(project, str(file), 3500 + i, mcg_content, {
                        "source": "digest", "project": project, "type": "method_call_graph",
                        "name": bean.get("name", "bean"), "class_name": bean.get("name"),
                    }))
                # Method body excerpts — one chunk per non-trivial service method
                method_bodies = bean.get("method_bodies", {})
                for j, (mname, mbody) in enumerate(list(method_bodies.items())[:8]):
                    mb_content = (
                        f"Implementation of {bean.get('name')}.{mname}() [{project}]:\n"
                        f"{mbody}"
                    )
                    rows.append(self._chunk_dict(project, str(file), 3600 + i * 10 + j, mb_content, {
                        "source": "digest", "project": project, "type": "method_body",
                        "name": bean.get("name", "bean"), "class_name": bean.get("name"),
                        "method_name": mname,
                    }))

            # Exception handlers
            for i, eh in enumerate(data.get("exception_handlers", [])):
                content = (
                    f"ExceptionHandler {eh.get('advice_class')}\n"
                    f"handles={eh.get('handled_exceptions')}"
                )
                rows.append(self._chunk_dict(project, str(file), 4000 + i, content, {
                    "source": "digest", "project": project, "type": "exception_handler",
                    "name": eh.get("advice_class", "handler"),
                }))

            # Scheduled tasks
            for i, task in enumerate(data.get("scheduled_tasks", [])):
                content = (
                    f"ScheduledTask class={task.get('class_name')} method={task.get('method')}\n"
                    f"schedule={task.get('schedule')}"
                )
                rows.append(self._chunk_dict(project, str(file), 4500 + i, content, {
                    "source": "digest", "project": project, "type": "scheduled_task",
                    "name": task.get("method", "task"),
                }))

            # DB migrations
            for i, migration in enumerate(data.get("db_migrations", [])):
                rows.append(self._chunk_dict(project, str(file), 4800 + i, f"Migration: {migration}", {
                    "source": "digest", "project": project, "type": "migration", "name": f"migration_{i}",
                }))

            # Build dependencies (one combined chunk)
            build_deps = data.get("build_dependencies", [])
            if build_deps:
                rows.append(self._chunk_dict(project, str(file), 4900, f"Build dependencies:\n" + "\n".join(build_deps), {
                    "source": "digest", "project": project, "type": "build_deps", "name": "dependencies",
                }))

            # DTO schemas (request/response field structures)
            for i, dto in enumerate(data.get("dto_schemas", [])):
                fields_text = "\n".join(
                    f"  {f['name']}: {f['type']}"
                    + (" [required]" if f.get("required") else "")
                    + (f" @JsonProperty({f['json_property']!r})" if f.get("json_property") else "")
                    + (f" validations={f['validations']}" if f.get("validations") else "")
                    for f in dto.get("fields", [])
                )
                feign_refs = self._find_dto_feign_refs(data, dto.get("name", ""))
                content = (
                    f"DTO {dto.get('name')} [{project}] file={dto.get('file_path','')}\n"
                    f"fields:\n{fields_text or '  (none extracted)'}"
                )
                if feign_refs:
                    content += f"\nUsed in Feign calls: {', '.join(feign_refs)}"
                rows.append(self._chunk_dict(project, str(file), 8000 + i, content, {
                    "source": "digest", "project": project, "type": "dto_schema",
                    "name": dto.get("name", "dto"), "class_name": dto.get("name", ""),
                }))

            # Angular components
            for i, comp in enumerate(data.get("components", [])):
                content = (
                    f"Component {comp.get('name')} selector={comp.get('selector')} file={comp.get('file_path')}\n"
                    f"inputs={comp.get('inputs')} outputs={comp.get('outputs')}\n"
                    f"services={comp.get('injected_services')}"
                )
                if comp.get("template_events"):
                    content += f"\ntemplate_events={comp['template_events']}"
                if comp.get("view_children"):
                    content += f"\nview_children={comp['view_children']}"
                if comp.get("methods"):
                    content += f"\nmethods={comp['methods']}"
                method_calls = comp.get("method_calls", {})
                if method_calls:
                    call_lines = [
                        f"  {m}() → {', '.join(calls)}"
                        for m, calls in list(method_calls.items())[:10]
                    ]
                    content += "\nmethod_call_graph:\n" + "\n".join(call_lines)
                rows.append(self._chunk_dict(project, str(file), 5000 + i, content, {
                    "source": "digest", "project": project, "type": "component",
                    "name": comp.get("name", "component"),
                }))

            # Angular services
            for i, svc in enumerate(data.get("services", [])):
                calls_summary = "; ".join(
                    f"{c.get('method')} {c.get('url')}" for c in svc.get("http_calls", [])
                )
                content = (
                    f"AngularService {svc.get('name')} file={svc.get('file_path')}\n"
                    f"http_calls={calls_summary}\n"
                    f"dependencies={svc.get('injected_dependencies')}"
                )
                rows.append(self._chunk_dict(project, str(file), 6000 + i, content, {
                    "source": "digest", "project": project, "type": "angular_service",
                    "name": svc.get("name", "service"),
                }))

            # NgRx features
            for i, feat in enumerate(data.get("ngrx_features", [])):
                content = (
                    f"NgRxFeature {feat.get('name')}\n"
                    f"actions={feat.get('actions')}\n"
                    f"effects={feat.get('effects')}\n"
                    f"selectors={feat.get('selectors')}"
                )
                rows.append(self._chunk_dict(project, str(file), 7000 + i, content, {
                    "source": "digest", "project": project, "type": "ngrx",
                    "name": feat.get("name", "feature"),
                }))

            # Master digest special sections
            if file.name == "master.digest.json":
                auth = data.get("auth_flow", {})
                rows.append(self._chunk_dict("master", str(file), 0, f"Auth flow: {auth}", {
                    "source": "digest", "project": "master", "type": "auth_flow", "name": "auth_flow",
                }))
                contracts = data.get("api_contracts", [])
                if contracts:
                    contract_text = "\n".join(
                        f"{c.get('caller')} -> {c.get('service')} : {c.get('endpoint')} via {c.get('angular_service')}"
                        for c in contracts
                    )
                    rows.append(self._chunk_dict("master", str(file), 1, f"API contracts:\n{contract_text}", {
                        "source": "digest", "project": "master", "type": "api_contracts", "name": "contracts",
                    }))

        return rows

    # ------------------------------------------------------------------
    # Graph relationship chunks (Phase 2)
    # ------------------------------------------------------------------

    def _graph_chunks(self) -> list[dict]:
        """
        Create retrieval-friendly chunks from the knowledge graph.
        Each chunk describes outgoing relationships from one node so that
        questions like 'what calls Order?' can surface the answer via RAG.
        """
        graph_path = self.root / "graph" / "knowledge_graph.json"
        if not graph_path.exists():
            return []

        try:
            data = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception:
            return []

        nodes: dict[str, dict] = data.get("nodes", {})
        edges: list[dict] = data.get("edges", [])

        # Group edges by source node
        from collections import defaultdict
        out_edges: dict[str, list[dict]] = defaultdict(list)
        in_edges: dict[str, list[dict]] = defaultdict(list)
        for edge in edges:
            out_edges[edge["from"]].append(edge)
            in_edges[edge["to"]].append(edge)

        rows: list[dict] = []

        for nid, node in nodes.items():
            # user_function nodes get richer chunks from _feature_chunks(); skip here
            if node.get("type") == "user_function":
                continue

            project = node.get("project", "master")
            name = node.get("name", nid)
            label = node.get("label", nid)

            # Outgoing relationships
            outs = out_edges.get(nid, [])
            ins = in_edges.get(nid, [])
            if not outs and not ins:
                continue

            lines = [f"Knowledge graph: {label}"]
            for edge in outs[:12]:
                target = nodes.get(edge["to"], {})
                lines.append(f"  --[{edge['type']}]--> {target.get('label', edge['to'])}")
            for edge in ins[:8]:
                src = nodes.get(edge["from"], {})
                lines.append(f"  <--[{edge['type']}]-- {src.get('label', edge['from'])}")

            # Use full 64-bit hash (no modulo) — negligible collision probability
            rows.append(self._chunk_dict(
                project,
                "graph/nodes",
                abs(hash(nid)),
                "\n".join(lines),
                {
                    "source": "graph", "project": project,
                    "type": f"graph_{node.get('type', 'node')}",
                    "name": name, "class_name": name,
                },
            ))

        return rows

    # ------------------------------------------------------------------
    # User-function feature chunks (one chunk per detected user function)
    # ------------------------------------------------------------------

    def _feature_chunks(self) -> list[dict]:
        """
        Generate one descriptive chunk per user_function node in the knowledge graph.
        Traverses part_of_feature, feature_uses, feature_calls edges to build a complete picture.
        """
        from collections import defaultdict as _dd

        graph_path = self.root / "graph" / "knowledge_graph.json"
        if not graph_path.exists():
            return []
        try:
            data = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception:
            return []

        nodes: dict[str, dict] = data.get("nodes", {})
        edges: list[dict] = data.get("edges", [])

        out_edges: dict[str, list[dict]] = _dd(list)
        in_edges: dict[str, list[dict]] = _dd(list)
        for edge in edges:
            out_edges[edge["from"]].append(edge)
            in_edges[edge["to"]].append(edge)

        rows: list[dict] = []
        for nid, node in nodes.items():
            if node.get("type") != "user_function":
                continue

            project = node.get("project", "")
            name = node.get("name", "")
            lines = [f"User Function: {name} [{project}]", ""]

            # Entry components
            entry_comps = node.get("entry_components", [])
            if entry_comps:
                lines.append(f"Entry components: {', '.join(entry_comps)}")

            # All components that are part of this feature
            all_comps = [
                nodes[e["from"]].get("name", "")
                for e in in_edges.get(nid, [])
                if e["type"] == "part_of_feature" and e["from"] in nodes
            ]
            if len(all_comps) > len(entry_comps):
                lines.append(f"All components ({len(all_comps)}): {', '.join(all_comps[:12])}")

            # Angular services
            ang_services = node.get("angular_services", [])
            if ang_services:
                lines.append(f"Angular services used: {', '.join(ang_services)}")

            # Backend microservices
            backend_projects = node.get("backend_projects", [])
            if backend_projects:
                lines.append(f"Backend microservices: {', '.join(backend_projects)}")

            # Spring services (follow feature_calls edges)
            spring_svcs: list[str] = []
            repos: list[str] = []
            for edge in out_edges.get(nid, []):
                if edge["type"] != "feature_calls":
                    continue
                target = nodes.get(edge["to"])
                if not target:
                    continue
                svc_name = target.get("name", "")
                methods = target.get("methods", [])[:5]
                spring_svcs.append(
                    f"{svc_name}({', '.join(methods)})" if methods else svc_name
                )
                # Follow depends_on / manages to repos and entities
                for dep_edge in out_edges.get(edge["to"], []):
                    if dep_edge["type"] in ("depends_on", "manages"):
                        dep = nodes.get(dep_edge["to"])
                        if dep and dep.get("type") in ("spring_repository", "entity"):
                            dep_name = dep.get("name", "")
                            dep_methods = dep.get("methods", [])[:4]
                            repos.append(
                                f"{dep_name}({', '.join(dep_methods)})" if dep_methods else dep_name
                            )

            if spring_svcs:
                lines.append(f"Backend services: {'; '.join(spring_svcs)}")
            if repos:
                lines.append(f"Repositories/Entities: {', '.join(set(repos))}")

            content = "\n".join(lines)
            # Use "graph/features" prefix — distinct from "graph/nodes" in _graph_chunks()
            # so IDs can never collide between the two methods.
            rows.append(self._chunk_dict(
                project,
                "graph/features",
                abs(hash(nid)),
                content,
                {
                    "source": "graph",
                    "project": project,
                    "type": "user_function",
                    "name": name,
                    "feature_key": node.get("feature_key", ""),
                    "class_name": name,
                },
            ))

        return rows

    # ------------------------------------------------------------------
    # Source code chunks (semantic boundary splitting)
    # ------------------------------------------------------------------

    def _code_chunks(self) -> list[dict]:
        rows: list[dict] = []
        for file in self.root.rglob("*"):
            if not file.is_file():
                continue
            if any(part.startswith(".") for part in file.parts):
                continue
            suffix = file.suffix.lower()
            if suffix not in {".java", ".ts", ".html", ".yml", ".yaml", ".properties", ".sql"}:
                continue
            parts_set = set(file.parts)
            if any(skip in parts_set for skip in ("digests", "vector_db", "vscode-extension", "node_modules", "target", "build", ".gradle")):
                continue
            rel = str(file.relative_to(self.root))
            project = rel.split("/")[0] if "/" in rel else "local-ai-agent"
            try:
                content = file.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # Use semantic splitting for Java and TypeScript
            if suffix == ".java":
                pieces = self._split_java(content)
            elif suffix == ".ts":
                pieces = self._split_typescript(content)
            else:
                pieces = self._split_content(content)

            for idx, piece in enumerate(pieces):
                rows.append(self._chunk_dict(
                    project, rel, idx, piece,
                    {
                        "source": "code", "project": project, "type": suffix.lstrip("."),
                        "file_path": rel, "class_name": self._guess_class(piece),
                        "method_name": self._guess_method(piece),
                    },
                ))
        return rows

    def _split_java(self, content: str) -> list[str]:
        """Split Java files at method boundaries for better semantic chunks."""
        # Split into class-level sections first
        method_pattern = re.compile(
            r"(?:(?:/\*\*[\s\S]*?\*/\s*)?(?:@\w+[^\n]*\n\s*)*"
            r"(?:public|private|protected|static|final|synchronized|abstract)\s+[\w<>\[\],\s]+\s+\w+\s*\([^)]*\)"
            r"(?:\s+throws\s+[\w,\s]+)?\s*\{)",
            re.MULTILINE,
        )
        boundaries = [m.start() for m in method_pattern.finditer(content)]
        if len(boundaries) < 2:
            return self._split_content(content)

        pieces: list[str] = []
        # Class header
        header = content[: boundaries[0]]
        if header.strip():
            pieces.append(header)
        # Each method
        for i, start in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < len(boundaries) else len(content)
            piece = content[start:end]
            if len(piece.strip()) > 20:
                pieces.extend(self._split_content(piece))
        return pieces or self._split_content(content)

    def _split_typescript(self, content: str) -> list[str]:
        """Split TypeScript files at function/class/method boundaries."""
        fn_pattern = re.compile(
            r"(?:(?:\/\/[^\n]*\n\s*)*)"
            r"(?:export\s+)?(?:async\s+)?(?:function\s+\w+|(?:public|private|protected|readonly)\s+(?:async\s+)?(?:\w+)\s*\(|(?:\w+)\s*=\s*(?:async\s+)?\()",
            re.MULTILINE,
        )
        boundaries = [m.start() for m in fn_pattern.finditer(content)]
        if len(boundaries) < 2:
            return self._split_content(content)

        pieces: list[str] = []
        header = content[: boundaries[0]]
        if header.strip():
            pieces.append(header)
        for i, start in enumerate(boundaries):
            end = boundaries[i + 1] if i + 1 < len(boundaries) else len(content)
            piece = content[start:end]
            if len(piece.strip()) > 20:
                pieces.extend(self._split_content(piece))
        return pieces or self._split_content(content)

    def _split_content(self, content: str, target_tokens: int = 500, overlap_tokens: int = 50) -> list[str]:
        if self.encoder is not None:
            toks = self.encoder.encode(content)
            if len(toks) <= 600:
                return [content]
            chunks = []
            start = 0
            while start < len(toks):
                end = min(start + target_tokens, len(toks))
                chunks.append(self.encoder.decode(toks[start:end]))
                if end == len(toks):
                    break
                start = max(0, end - overlap_tokens)
            return chunks

        target_chars = target_tokens * 4
        overlap_chars = overlap_tokens * 4
        if len(content) <= target_chars + overlap_chars:
            return [content]
        chunks = []
        start = 0
        while start < len(content):
            end = min(start + target_chars, len(content))
            chunks.append(content[start:end])
            if end == len(content):
                break
            start = max(0, end - overlap_chars)
        return chunks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _guess_class(content: str) -> str:
        import re
        m = re.search(r"(?:class|interface|enum)\s+([A-Za-z0-9_]+)", content)
        return m.group(1) if m else ""

    @staticmethod
    def _guess_method(content: str) -> str:
        import re
        m = re.search(r"(?:public|private|protected|async)\s+[A-Za-z0-9_<>\[\]]+\s+([A-Za-z0-9_]+)\s*\(", content)
        return m.group(1) if m else ""

    @staticmethod
    def _chunk_dict(project: str, file_path: str, idx: int, content: str, metadata: dict[str, Any]) -> dict:
        return {"id": f"{project}::{file_path}::{idx}", "content": content, "metadata": metadata}

    @staticmethod
    def _find_dto_feign_refs(digest_data: dict, dto_name: str) -> list[str]:
        """Find which Feign client calls reference this DTO as request or response."""
        refs: list[str] = []
        for fc in digest_data.get("feign_clients", []):
            for cd in fc.get("call_details", []):
                if dto_name in (cd.get("request_dto", ""), cd.get("response_dto", "")):
                    refs.append(f"{fc['client_name']}.{cd.get('method','')} {cd.get('path','')}")
        return refs

