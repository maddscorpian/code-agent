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
        self.load()

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def load(self) -> None:
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
            lines.append(f"  {ep['label']}")
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
