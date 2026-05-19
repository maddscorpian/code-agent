from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path


class GraphStore:
    """
    Loads the knowledge graph from disk and provides traversal-based query tools.
    All public methods return human-readable strings for direct use in LLM prompts.
    """

    def __init__(self, graph_path: str):
        self.graph_path = Path(graph_path)
        self._nodes: dict[str, dict] = {}
        self._edges: list[dict] = []
        self._out: dict[str, list[dict]] = defaultdict(list)   # node_id → outgoing edges
        self._in: dict[str, list[dict]] = defaultdict(list)    # node_id → incoming edges
        self._digest_query_cache: dict | None = None            # {project: {class_lower: [queries]}}
        self.load()

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def load(self) -> None:
        self._digest_query_cache = None   # invalidate on reload
        if not self.graph_path.exists():
            return
        try:
            data = json.loads(self.graph_path.read_text(encoding="utf-8"))
            self._nodes = data.get("nodes", {})
            self._edges = data.get("edges", [])
            self._out.clear()
            self._in.clear()
            for edge in self._edges:
                self._out[edge["from"]].append(edge)
                self._in[edge["to"]].append(edge)
        except Exception:
            pass

    def is_empty(self) -> bool:
        return len(self._nodes) == 0

    def stats(self) -> dict:
        return {
            "nodes": len(self._nodes),
            "edges": len(self._edges),
        }

    # ------------------------------------------------------------------
    # Public query tools (all return str for LLM consumption)
    # ------------------------------------------------------------------

    def trace_request(self, query: str) -> str:
        """
        Trace a full request path for an endpoint.
        Input: endpoint path (e.g. "/api/orders") or "METHOD /path" (e.g. "GET /api/orders")
        Output: layered trace — Angular callers → controller → service bean → repo → entity → Kafka events.
        """
        if self.is_empty():
            return "Knowledge graph not built. Run /reindex to build it."

        endpoint_nodes = self._find_endpoint_nodes(query)
        if not endpoint_nodes:
            return f"No endpoint found matching: '{query}'. Try a path segment like '/orders' or 'GET /api/orders'."

        lines: list[str] = []
        for ep in endpoint_nodes[:3]:   # at most 3 matching endpoints
            lines.append(f"\n{'='*60}")
            lines.append(f"TRACE: {ep['label']}")
            lines.append("=" * 60)

            # ── Angular callers (backward from endpoint)
            angular_callers = self._bfs_backward(
                ep["id"], stop_types={"angular_service", "angular_component"}, max_depth=3
            )
            if angular_callers:
                lines.append("\n[Angular Callers]")
                for node in angular_callers:
                    lines.append(f"  {node.get('label', node['id'])}")
                    # Who uses this Angular service?
                    parent_components = [
                        self._nodes[e["from"]]
                        for e in self._in.get(node["id"], [])
                        if e["type"] == "uses_service" and e["from"] in self._nodes
                    ]
                    for pc in parent_components:
                        lines.append(f"    ↑ used by: {pc.get('label', pc['id'])}")

            # ── Feign callers (other services calling this endpoint)
            feign_callers = [
                self._nodes[e["from"]]
                for e in self._in.get(ep["id"], [])
                if e["type"] == "feign_calls" and e["from"] in self._nodes
            ]
            if feign_callers:
                lines.append("\n[Feign Callers (inter-service)]")
                for fc in feign_callers:
                    lines.append(f"  {fc.get('label', fc['id'])}")

            # ── Backend chain (forward from endpoint)
            lines.append("\n[Backend Chain]")
            if ep.get("auth_required") and ep.get("roles"):
                auth_str = f"  [AUTH: required, roles={ep['roles']}]"
            elif ep.get("auth_required"):
                auth_str = "  [AUTH: required]"
            else:
                auth_str = "  [AUTH: public]"
            lines.append(f"  {ep['label']}{auth_str}")
            self._format_forward_chain(ep["id"], lines, indent=4, visited=set(), max_depth=5)

        return "\n".join(lines)

    def find_callers(self, query: str) -> str:
        """
        Find everything that calls a given endpoint, bean, or service.
        Input: class name ("OrderService"), path ("/api/orders"), or "project::name"
        """
        if self.is_empty():
            return "Knowledge graph not built. Run /reindex to build it."

        target_nodes = self._find_nodes_by_name(query)
        if not target_nodes:
            return f"No node found matching: '{query}'"

        lines: list[str] = []
        for node in target_nodes[:3]:
            lines.append(f"\n[Callers of: {node['label']}]")

            incoming = self._in.get(node["id"], [])
            if not incoming:
                lines.append("  (no known callers)")
                continue

            by_type: dict[str, list[str]] = defaultdict(list)
            for edge in incoming:
                caller = self._nodes.get(edge["from"])
                if caller:
                    by_type[edge["type"]].append(caller.get("label", edge["from"]))

            for edge_type, callers in by_type.items():
                lines.append(f"  via {edge_type}:")
                for c in callers:
                    lines.append(f"    • {c}")

        return "\n".join(lines)

    def impact_graph(self, query: str) -> str:
        """
        BFS impact analysis: find everything affected by a change to this class/entity/endpoint.
        Input: class name, entity name, or endpoint path.
        """
        if self.is_empty():
            return "Knowledge graph not built. Run /reindex to build it."

        start_nodes = self._find_nodes_by_name(query)
        if not start_nodes:
            return f"No node found matching: '{query}'"

        node = start_nodes[0]
        lines: list[str] = [
            f"\n{'='*60}",
            f"IMPACT ANALYSIS: {node['label']}",
            "=" * 60,
        ]

        # Forward impact (things that depend on this node)
        forward = self._bfs_forward(node["id"], max_depth=4)
        if forward:
            lines.append("\n[Downstream Impact — depends on this node]")
            self._format_impact_group(forward, lines)

        # Backward impact (things this node depends on — informational)
        backward = self._bfs_backward(node["id"], max_depth=3)
        if backward:
            lines.append("\n[Upstream Dependencies — this node relies on]")
            self._format_impact_group(backward, lines)

        # Kafka events connected to this node (both directions)
        kafka_nodes = [
            self._nodes[e["to"]]
            for e in self._out.get(node["id"], [])
            if e["type"] == "produces_event" and e["to"] in self._nodes
        ] + [
            self._nodes[e["from"]]
            for e in self._in.get(node["id"], [])
            if e["type"] == "consumes_event" and e["from"] in self._nodes
        ]
        if kafka_nodes:
            lines.append("\n[Kafka Events]")
            for kn in kafka_nodes:
                # Who else consumes this topic?
                consumers = [
                    self._nodes[e["to"]]
                    for e in self._out.get(kn["id"], [])
                    if e["type"] == "consumes_event" and e["to"] in self._nodes
                ]
                producers = [
                    self._nodes[e["from"]]
                    for e in self._in.get(kn["id"], [])
                    if e["type"] == "produces_event" and e["from"] in self._nodes
                ]
                lines.append(f"  Topic: {kn['name']}")
                for p in producers:
                    lines.append(f"    produced by: {p.get('label', p['id'])}")
                for c in consumers:
                    lines.append(f"    consumed by: {c.get('label', c['id'])}")

        # Risk hint
        total_impacted = len(forward) + len(backward)
        risk = "HIGH" if total_impacted > 8 else ("MEDIUM" if total_impacted > 3 else "LOW")
        lines.append(f"\n[Risk Level: {risk}] — {total_impacted} artifacts directly connected")

        return "\n".join(lines)

    def list_features(self, project_filter: str = "") -> str:
        """List all detected user functions (features) with their entry components and backend projects."""
        if self.is_empty():
            return "Knowledge graph not built. Run /reindex to build it."

        feature_nodes = [
            n for n in self._nodes.values()
            if n.get("type") == "user_function"
            and (not project_filter or n.get("project", "").lower() == project_filter.lower())
        ]

        if not feature_nodes:
            return (
                f"No user functions found{f' for {project_filter}' if project_filter else ''}. "
                "Run /reindex to rebuild the graph."
            )

        lines = [f"User Functions ({len(feature_nodes)} detected):"]
        for fn in sorted(feature_nodes, key=lambda x: x.get("name", "")):
            entry = fn.get("entry_components", [])[:2]
            backends = fn.get("backend_projects", [])
            line = f"\n  • {fn['name']} [{fn.get('project', '')}]"
            if entry:
                line += f"\n    entry: {', '.join(entry)}"
            if backends:
                line += f"\n    backend: {', '.join(backends)}"
            svc_count = len(fn.get("angular_services", []))
            if svc_count:
                line += f"\n    angular services: {svc_count}"
            lines.append(line)

        return "\n".join(lines)

    @staticmethod
    def _normalize_feature_query(raw: str) -> str:
        """
        Normalize a user query to a comparable feature name.
        'BookAppointmentSlotModule' → 'book appointment slot'
        'book-appointment-slot'    → 'book appointment slot'
        """
        import re
        q = raw.strip()
        # Strip known Angular/Java suffixes
        for suffix in ("Module", "Component", "Service", "Feature", "Page", "View",
                       "module", "component", "service", "feature", "page", "view"):
            if q.endswith(suffix) and len(q) > len(suffix):
                q = q[:-len(suffix)]
                break
        # Split PascalCase / camelCase into words
        words = re.findall(r'[A-Za-z][a-z]*', q)
        if words:
            return " ".join(w.lower() for w in words)
        # Hyphen/underscore separated
        return q.lower().replace("-", " ").replace("_", " ")

    def describe_feature(self, feature_name: str) -> str:
        """Full end-to-end trace for a user function: Angular → services → Spring backend → repos."""
        if self.is_empty():
            return "Knowledge graph not built. Run /reindex to build it."

        feature_nodes = [n for n in self._nodes.values() if n.get("type") == "user_function"]
        raw_lower = feature_name.lower().strip()
        normalized = self._normalize_feature_query(feature_name)  # e.g. "book appointment slot"

        match = None

        # 1. Exact name match (case-insensitive)
        match = next((fn for fn in feature_nodes
                      if fn.get("name", "").lower() == raw_lower
                      or fn.get("name", "").lower() == normalized), None)

        # 2. Substring match (both directions)
        if not match:
            match = next((fn for fn in feature_nodes
                          if raw_lower in fn.get("name", "").lower()
                          or normalized in fn.get("name", "").lower()
                          or fn.get("name", "").lower() in raw_lower
                          or fn.get("feature_key", "").replace("-", " ") in normalized
                          or normalized in fn.get("feature_key", "").replace("-", " ")), None)

        # 3. Word-overlap fallback — pick the feature with the most words in common
        if not match and normalized:
            query_words = set(normalized.split())
            scored = [
                (fn, len(query_words & set(fn.get("name", "").lower().split())))
                for fn in feature_nodes
            ]
            best_fn, best_score = max(scored, key=lambda x: x[1], default=(None, 0))
            if best_score >= 2:
                match = best_fn

        if not match:
            available = ", ".join(fn.get("name", "") for fn in feature_nodes[:15])
            return (
                f"User function '{feature_name}' not found.\n"
                f"Normalized query: '{normalized}'\n"
                f"Available features: {available}"
            )

        nid = match["id"]
        project = match.get("project", "")
        name = match.get("name", "")

        lines = [
            f"\n{'='*60}",
            f"User Function: {name} [{project}]",
            "=" * 60,
        ]

        # All components in this feature (incoming part_of_feature edges)
        all_comps = [
            self._nodes[e["from"]]
            for e in self._in.get(nid, [])
            if e["type"] == "part_of_feature" and e["from"] in self._nodes
        ]
        entry_comps = set(match.get("entry_components", []))
        if all_comps:
            lines.append("\n[Angular Components]")
            for comp in all_comps[:10]:
                comp_name = comp.get("name", "")
                marker = "* " if comp_name in entry_comps else "  "
                lines.append(f"  {marker}{comp_name}")
                services = comp.get("injected_services", [])
                if services:
                    lines.append(f"      uses: {', '.join(services[:4])}")
            if len(all_comps) > 10:
                lines.append(f"  ... and {len(all_comps) - 10} more components")

        # Angular services (feature_uses edges)
        ang_svc_nodes = [
            self._nodes[e["to"]]
            for e in self._out.get(nid, [])
            if e["type"] == "feature_uses" and e["to"] in self._nodes
        ]
        if ang_svc_nodes:
            lines.append("\n[Angular Services]")
            for svc in ang_svc_nodes:
                http_calls = svc.get("http_calls", [])
                lines.append(f"  {svc.get('name', '')}")
                for call in http_calls[:3]:
                    lines.append(f"    → {call.get('method', 'GET')} {call.get('url', '')}")

        # Spring Endpoints — traverse http_call / inferred_http_call edges from angular services
        seen_ep_ids: set[str] = set()
        endpoint_nodes: list[dict] = []
        for svc_node in ang_svc_nodes:
            for edge in self._out.get(svc_node["id"], []):
                if edge["type"] in ("http_call", "inferred_http_call"):
                    ep = self._nodes.get(edge["to"])
                    if ep and ep.get("type") == "endpoint" and ep["id"] not in seen_ep_ids:
                        seen_ep_ids.add(ep["id"])
                        endpoint_nodes.append(ep)
        if endpoint_nodes:
            lines.append("\n[Spring Endpoints]")
            for ep in endpoint_nodes[:8]:
                auth_str = ""
                if ep.get("auth_required"):
                    roles = ep.get("roles", [])
                    auth_str = f"  [AUTH: {', '.join(roles) if roles else 'required'}]"
                else:
                    auth_str = "  [AUTH: public]"
                ctrl = ep.get("controller", "")
                ep_project = ep.get("project", "")
                req = ep.get("request_dto", "")
                resp = ep.get("response_dto", "")
                dto_str = ""
                if req:
                    dto_str += f"  request={req}"
                if resp:
                    dto_str += f"  response={resp}"
                lines.append(
                    f"  {ep.get('method','GET')} {ep.get('path','')} "
                    f"[{ctrl}, {ep_project}]{auth_str}"
                )
                if dto_str:
                    lines.append(f"    {dto_str.strip()}")

        # Spring backend (feature_calls edges)
        spring_svc_nodes = [
            self._nodes[e["to"]]
            for e in self._out.get(nid, [])
            if e["type"] == "feature_calls" and e["to"] in self._nodes
        ]
        if spring_svc_nodes:
            by_project: dict[str, list[dict]] = defaultdict(list)
            for svc in spring_svc_nodes:
                by_project[svc.get("project", "unknown")].append(svc)

            for proj, svcs in sorted(by_project.items()):
                lines.append(f"\n[Backend: {proj}]")
                for svc in svcs:
                    methods = svc.get("methods", [])[:6]
                    lines.append(f"  {svc.get('name', '')}")
                    if methods:
                        lines.append(f"    methods: {', '.join(methods)}")
                    # Repos and entities this service depends on
                    for dep_edge in self._out.get(svc["id"], []):
                        dep = self._nodes.get(dep_edge["to"])
                        if dep and dep.get("type") in ("spring_repository", "entity"):
                            dep_methods = dep.get("methods", [])[:4]
                            lines.append(
                                f"    [{dep.get('type', '')}] {dep.get('name', '')}"
                                + (f": {', '.join(dep_methods)}" if dep_methods else "")
                            )
                            if dep.get("type") == "spring_repository":
                                queries = self._get_digest_queries(
                                    dep.get("project", ""), dep.get("name", "")
                                )
                                for q in queries[:3]:
                                    lines.append(f"      @Query: {q}")

        # Kafka / RabbitMQ events — traverse feature_calls beans and their project peers
        # The user_function node itself has no event edges; they are on the Spring service beans.
        producing_beans: list[tuple[str, str]] = []   # (bean_name, topic_name)
        consuming_beans: list[tuple[str, str]] = []

        visited_event_beans: set[str] = set()

        def _collect_events_from_bean(bean_id: str) -> None:
            if bean_id in visited_event_beans:
                return
            visited_event_beans.add(bean_id)
            bean = self._nodes.get(bean_id, {})
            bname = bean.get("name", "?")
            for edge in self._out.get(bean_id, []):
                if edge["type"] == "produces_event":
                    topic = self._nodes.get(edge["to"], {}).get("name", edge["to"])
                    producing_beans.append((bname, topic))
            for edge in self._in.get(bean_id, []):
                if edge["type"] == "consumes_event":
                    topic = self._nodes.get(edge["from"], {}).get("name", edge["from"])
                    consuming_beans.append((bname, topic))

        # Walk all spring service beans reached by feature_calls
        for edge in self._out.get(nid, []):
            if edge["type"] != "feature_calls":
                continue
            bean_id = edge["to"]
            _collect_events_from_bean(bean_id)
            # Also check other beans in the same project (sibling beans may handle events)
            proj = self._nodes.get(bean_id, {}).get("project", "")
            if proj:
                for sibling_id, sibling in self._nodes.items():
                    if (sibling.get("project") == proj
                            and sibling.get("type") == "spring_service"
                            and sibling_id not in visited_event_beans):
                        # Only include if it has event edges (don't flood with all beans)
                        has_events = any(
                            e["type"] in ("produces_event", "consumes_event")
                            for e in self._out.get(sibling_id, []) + self._in.get(sibling_id, [])
                        )
                        if has_events:
                            _collect_events_from_bean(sibling_id)

        lines.append("\n[Events]")
        if producing_beans:
            lines.append("  Produces:")
            for bean, topic in producing_beans:
                lines.append(f"    {bean} → {topic}")
        else:
            lines.append("  Produces: (none detected — topics may use @Value or constants; reindex to capture)")

        if consuming_beans:
            lines.append("  Consumes:")
            for bean, topic in consuming_beans:
                lines.append(f"    {bean} ← {topic}")
        else:
            lines.append("  Consumes: (none detected)")

        return "\n".join(lines)

    def trace_event_flow(self, query: str) -> str:
        """
        Trace the complete flow for a Kafka topic or event-related query.
        Input: topic name (e.g. "order.events"), partial match, or service name.
        Returns: publisher endpoint(s) → topic → all consumer services with handler beans.
        """
        if self.is_empty():
            return "Knowledge graph not built. Run /reindex to build it."

        query_lower = query.lower().strip()

        # Find matching kafka_topic nodes
        topic_nodes = [
            n for n in self._nodes.values()
            if n.get("type") == "kafka_topic"
            and (query_lower in n.get("name", "").lower()
                 or n.get("name", "").lower() in query_lower)
        ]

        if not topic_nodes:
            # Try to find by service name — return all topics for that service
            service_beans = [
                n for n in self._nodes.values()
                if n.get("type") in ("spring_service", "spring_component")
                and query_lower in n.get("project", "").lower()
            ]
            if service_beans:
                # Collect topics from those beans
                for bean in service_beans:
                    for edge in self._out.get(bean["id"], []) + self._in.get(bean["id"], []):
                        if edge["type"] in ("produces_event", "consumes_event", "publishes_to"):
                            tn = self._nodes.get(
                                edge["to"] if edge["type"] != "consumes_event" else edge["from"]
                            )
                            if tn and tn not in topic_nodes:
                                topic_nodes.append(tn)

        if not topic_nodes:
            return (
                f"No Kafka topic found matching '{query}'. "
                "Try the topic name (e.g. 'order.events') or a service name."
            )

        lines: list[str] = []
        for topic_node in topic_nodes[:5]:
            tnid = topic_node["id"]
            topic_name = topic_node.get("name", tnid)

            lines.append(f"\n{'='*60}")
            lines.append(f"Kafka Event Flow: {topic_name}")
            lines.append("=" * 60)

            # ── Publishers (REST endpoints that publish via publishes_to edge)
            publisher_eps = [
                self._nodes[e["from"]]
                for e in self._in.get(tnid, [])
                if e["type"] == "publishes_to" and e["from"] in self._nodes
            ]
            # ── Producers (beans that produce via produces_event)
            producer_beans = [
                self._nodes[e["from"]]
                for e in self._in.get(tnid, [])
                if e["type"] == "produces_event" and e["from"] in self._nodes
            ]

            if publisher_eps or producer_beans:
                lines.append("\n[Producers]")
                for ep in publisher_eps:
                    lines.append(f"  REST → {ep.get('label', ep['id'])}")
                    # Who calls this endpoint? (Angular services, Feign callers)
                    callers = [
                        self._nodes[e["from"]]
                        for e in self._in.get(ep["id"], [])
                        if e["from"] in self._nodes
                    ]
                    for caller in callers[:3]:
                        lines.append(f"    ↑ called by: {caller.get('label', caller['id'])}")
                for bean in producer_beans:
                    lines.append(f"  Bean → {bean.get('label', bean['id'])}")
            else:
                lines.append("\n[Producers]")
                lines.append("  (none detected — reindex after adding @Value topic fields)")

            # ── Topic node
            lines.append(f"\n[Topic]  {topic_name}")

            # ── Consumers (beans that consume via consumes_event)
            consumer_beans = [
                self._nodes[e["to"]]
                for e in self._out.get(tnid, [])
                if e["type"] == "consumes_event" and e["to"] in self._nodes
            ]

            if consumer_beans:
                lines.append("\n[Consumers]")
                by_project: dict[str, list] = defaultdict(list)
                for bean in consumer_beans:
                    by_project[bean.get("project", "?")].append(bean)
                for proj, beans in sorted(by_project.items()):
                    lines.append(f"  {proj}:")
                    for bean in beans:
                        methods = bean.get("methods", [])[:4]
                        lines.append(
                            f"    {bean.get('name', '?')}"
                            + (f"  [{', '.join(methods)}]" if methods else "")
                        )
            else:
                lines.append("\n[Consumers]  (none detected)")

        return "\n".join(lines)

    def summary(self) -> str:
        """Return a short human-readable graph summary."""
        if self.is_empty():
            return "Knowledge graph is empty. Run /reindex to build it."

        node_counts: dict[str, int] = defaultdict(int)
        proj_counts: dict[str, int] = defaultdict(int)
        for n in self._nodes.values():
            node_counts[n.get("type", "?")] += 1
            if p := n.get("project"):
                proj_counts[p] += 1

        edge_counts: dict[str, int] = defaultdict(int)
        for e in self._edges:
            edge_counts[e["type"]] += 1

        lines = [
            f"Knowledge Graph: {len(self._nodes)} nodes, {len(self._edges)} edges",
            "",
            "Node types: " + ", ".join(f"{t}={c}" for t, c in sorted(node_counts.items())),
            "Edge types: " + ", ".join(f"{t}={c}" for t, c in sorted(edge_counts.items())),
            "Projects: " + ", ".join(f"{p}({c})" for p, c in sorted(proj_counts.items())),
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # BFS helpers
    # ------------------------------------------------------------------

    def _bfs_forward(self, start_id: str, max_depth: int = 4) -> list[dict]:
        """BFS following outgoing edges from start_id."""
        visited: set[str] = {start_id}
        result: list[dict] = []
        queue: list[tuple[str, int]] = [(start_id, 0)]
        while queue:
            nid, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for edge in self._out.get(nid, []):
                target = edge["to"]
                if target not in visited and target in self._nodes:
                    visited.add(target)
                    result.append(self._nodes[target])
                    queue.append((target, depth + 1))
        return result

    def _bfs_backward(self, start_id: str,
                      stop_types: set[str] | None = None,
                      max_depth: int = 4) -> list[dict]:
        """BFS following incoming edges from start_id."""
        visited: set[str] = {start_id}
        result: list[dict] = []
        queue: list[tuple[str, int]] = [(start_id, 0)]
        while queue:
            nid, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for edge in self._in.get(nid, []):
                src = edge["from"]
                if src not in visited and src in self._nodes:
                    visited.add(src)
                    node = self._nodes[src]
                    if stop_types is None or node.get("type") in stop_types:
                        result.append(node)
                    queue.append((src, depth + 1))
        return result

    def _get_digest_queries(self, project: str, class_name: str) -> list[str]:
        """Return @Query JPQL/SQL strings for a repository class.
        Reads all digest files once on first call; subsequent calls are O(1) dict lookups."""
        if self._digest_query_cache is None:
            self._digest_query_cache = {}
            digests_dir = self.graph_path.parent.parent / "digests"
            for digest_file in digests_dir.glob("*.digest.json"):
                if digest_file.name == "master.digest.json":
                    continue
                try:
                    data = json.loads(digest_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                proj = data.get("project", "")
                self._digest_query_cache.setdefault(proj, {})
                for bean in data.get("beans", []):
                    name_key = bean.get("name", "").lower()
                    queries = [q[:300] for q in bean.get("queries", [])[:5]]
                    if queries:
                        self._digest_query_cache[proj][name_key] = queries
        return self._digest_query_cache.get(project, {}).get(class_name.lower(), [])

    def _format_forward_chain(self, node_id: str, lines: list[str],
                              indent: int, visited: set[str], max_depth: int) -> None:
        if max_depth == 0 or node_id in visited:
            return
        visited.add(node_id)
        prefix = " " * indent
        for edge in self._out.get(node_id, []):
            target_id = edge["to"]
            target = self._nodes.get(target_id)
            if not target or target_id in visited:
                continue
            lines.append(f"{prefix}→ [{edge['type']}] {target.get('label', target_id)}")
            if target.get("type") == "entity":
                fields = target.get("fields", [])[:6]
                if fields:
                    lines.append(f"{prefix}  fields: {', '.join(fields)}")
            elif target.get("type") == "spring_repository":
                queries = self._get_digest_queries(target.get("project", ""), target.get("name", ""))
                if queries:
                    lines.append(f"{prefix}  @Query ({len(queries)}):")
                    for q in queries:
                        lines.append(f"{prefix}    {q}")
            self._format_forward_chain(target_id, lines, indent + 2, visited, max_depth - 1)

    @staticmethod
    def _format_impact_group(nodes: list[dict], lines: list[str]) -> None:
        by_type: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            by_type[n.get("type", "?")].append(n.get("label", n["id"]))
        for ntype, labels in sorted(by_type.items()):
            lines.append(f"  [{ntype}]")
            for lbl in labels:
                lines.append(f"    • {lbl}")

    # ------------------------------------------------------------------
    # Node lookup helpers
    # ------------------------------------------------------------------

    def _find_endpoint_nodes(self, query: str) -> list[dict]:
        query_lower = query.lower().strip()
        # Try "METHOD /path" format
        parts = query_lower.split(" ", 1)
        if len(parts) == 2 and parts[0] in {"get", "post", "put", "delete", "patch"}:
            method, path = parts[0].upper(), parts[1]
            exact = [
                n for n in self._nodes.values()
                if n.get("type") == "endpoint"
                and n.get("method") == method
                and self._norm_path(n.get("path", "")) == self._norm_path(path)
            ]
            if exact:
                return exact

        # Substring match on path
        return [
            n for n in self._nodes.values()
            if n.get("type") == "endpoint"
            and query_lower in n.get("path", "").lower()
        ]

    def _find_nodes_by_name(self, query: str) -> list[dict]:
        query_lower = query.lower().strip()

        # Exact name match
        exact = [n for n in self._nodes.values()
                 if n.get("name", "").lower() == query_lower]
        if exact:
            return exact

        # Substring match on name or label
        return [
            n for n in self._nodes.values()
            if query_lower in n.get("name", "").lower()
            or query_lower in n.get("label", "").lower()
            or query_lower in n.get("path", "").lower()
        ][:10]

    @staticmethod
    def _norm_path(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "*", path).rstrip("/") or "/"
