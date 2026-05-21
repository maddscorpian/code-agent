from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from graph.feature_graph import FeatureGraphBuilder, _infer_backend_project


class GraphBuilder:
    """
    Builds a directed property graph from all digest files.

    Node types:
        angular_component, angular_service,
        endpoint, bean (spring_service / spring_repository / spring_component / spring_configuration / spring_advice),
        entity, kafka_topic

    Edge types:
        uses_service   angular_component  → angular_service
        http_call      angular_service    → endpoint
        handled_by     endpoint           → bean          (heuristic: controller name → service bean)
        depends_on     bean               → bean          (constructor / @Autowired injection)
        manages        bean(repository)   → entity
        jpa_relation   entity             → entity        (OneToMany / ManyToOne / etc.)
        feign_calls    bean               → endpoint      (cross-service Feign)
        produces_event bean               → kafka_topic
        consumes_event kafka_topic        → bean
        publishes_to   endpoint           → kafka_topic    (REST endpoint publishes directly)
    """

    def __init__(self, digests_dir: str):
        self.digests_dir = Path(digests_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self) -> dict:
        nodes: dict[str, dict] = {}
        edges: list[dict] = []

        service_digests, angular_digest, master_digest = self._load_all_digests()
        spring_projects = [svc["project"] for svc in service_digests]

        # Phase 1 – nodes (must be complete before building edges)
        for svc in service_digests:
            self._add_spring_nodes(nodes, svc)
        if angular_digest:
            self._add_angular_nodes(nodes, angular_digest)

        # Phase 2 – edges
        for svc in service_digests:
            edges.extend(self._build_spring_edges(nodes, svc))
        if angular_digest:
            edges.extend(self._build_angular_edges(nodes, angular_digest, spring_projects))
        if master_digest:
            edges.extend(self._build_master_edges(nodes, master_digest))

        # Phase 3 – user function graph (feature nodes + feature edges)
        if angular_digest:
            feature_builder = FeatureGraphBuilder(angular_digest, nodes, spring_projects)
            feature_nodes, feature_edges = feature_builder.build()
            nodes.update(feature_nodes)
            edges.extend(feature_edges)

        # Deduplicate edges
        seen: set[tuple] = set()
        unique_edges: list[dict] = []
        for e in edges:
            key = (e["from"], e["to"], e["type"])
            if key not in seen:
                seen.add(key)
                unique_edges.append(e)

        return {
            "nodes": nodes,
            "edges": unique_edges,
            "stats": {
                "nodes": len(nodes),
                "edges": len(unique_edges),
                "node_types": self._count_types(nodes.values(), "type"),
                "edge_types": self._count_types(unique_edges, "type"),
            },
            "built_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # Digest loading
    # ------------------------------------------------------------------

    def _load_all_digests(self) -> tuple[list[dict], dict | None, dict | None]:
        service_digests: list[dict] = []
        angular_digest: dict | None = None
        master_digest: dict | None = None

        for f in self.digests_dir.glob("*.digest.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if f.name == "master.digest.json":
                master_digest = data
            elif data.get("type") == "angular":
                angular_digest = data
            elif data.get("type") == "spring-boot":
                service_digests.append(data)

        return service_digests, angular_digest, master_digest

    # ------------------------------------------------------------------
    # Node creators
    # ------------------------------------------------------------------

    def _add_spring_nodes(self, nodes: dict, svc: dict) -> None:
        project = svc["project"]

        for ep in svc.get("endpoints", []):
            nid = self._ep_id(project, ep["method"], ep["path"])
            nodes[nid] = {
                "id": nid, "type": "endpoint", "project": project,
                "name": f"{ep['method']} {ep['path']}",
                "method": ep["method"], "path": ep["path"],
                "controller": ep.get("controller", ""),
                "handler": ep.get("handler", ""),
                "auth_required": ep.get("auth_required", False),
                "roles": ep.get("roles", []),
                "label": f"Endpoint {ep['method']} {ep['path']} [{project}]",
            }

        for bean in svc.get("beans", []):
            nid = self._bean_id(project, bean["name"])
            nodes[nid] = {
                "id": nid,
                "type": f"spring_{bean['bean_type']}",
                "project": project,
                "name": bean["name"],
                "bean_type": bean["bean_type"],
                "file_path": bean.get("file_path", ""),
                "methods": bean.get("methods", []),
                "transactional_methods": bean.get("transactional_methods", []),
                "dependencies": bean.get("dependencies", []),
                "label": f"{bean['bean_type'].capitalize()} {bean['name']} [{project}]",
            }

        for ent in svc.get("entities", []):
            nid = self._entity_id(project, ent["name"])
            nodes[nid] = {
                "id": nid, "type": "entity", "project": project,
                "name": ent["name"],
                "table": ent.get("table", ent["name"].lower()),
                "fields": ent.get("fields", []),
                "relationships": ent.get("relationships", []),
                "label": f"Entity {ent['name']} (table={ent.get('table', '')}) [{project}]",
            }

        for topic in set(svc.get("events", {}).get("produces", []) +
                         svc.get("events", {}).get("consumes", [])):
            nid = f"kafka_topic::{topic}"
            if nid not in nodes:
                nodes[nid] = {
                    "id": nid, "type": "kafka_topic",
                    "name": topic,
                    "label": f"Kafka topic: {topic}",
                }

    def _add_angular_nodes(self, nodes: dict, angular: dict) -> None:
        project = angular["project"]

        for comp in angular.get("components", []):
            nid = f"angular_component::{project}::{comp['name']}"
            nodes[nid] = {
                "id": nid, "type": "angular_component", "project": project,
                "name": comp["name"],
                "selector": comp.get("selector", ""),
                "file_path": comp.get("file_path", ""),
                "injected_services": comp.get("injected_services", []),
                "label": f"Component {comp['name']} [{project}]",
            }

        for svc in angular.get("services", []):
            nid = f"angular_service::{project}::{svc['name']}"
            nodes[nid] = {
                "id": nid, "type": "angular_service", "project": project,
                "name": svc["name"],
                "file_path": svc.get("file_path", ""),
                "http_calls": svc.get("http_calls", []),
                "label": f"Angular Service {svc['name']} [{project}]",
            }

    # ------------------------------------------------------------------
    # Edge builders
    # ------------------------------------------------------------------

    def _build_spring_edges(self, nodes: dict, svc: dict) -> list[dict]:
        edges: list[dict] = []
        project = svc["project"]

        # Bean → dependency (constructor injection)
        for bean in svc.get("beans", []):
            bean_nid = self._bean_id(project, bean["name"])
            for dep_name in bean.get("dependencies", []):
                dep_node = self._find_bean(nodes, dep_name, prefer_project=project)
                if dep_node:
                    edges.append(self._edge(bean_nid, dep_node["id"], "depends_on",
                                            f"{bean['name']} → {dep_node['name']}"))

        # Repository → entity (naming convention: OrderRepository → Order)
        for bean in svc.get("beans", []):
            if bean.get("bean_type") != "repository":
                continue
            bean_nid = self._bean_id(project, bean["name"])
            entity_name = re.sub(r"(Repository|Repo)$", "", bean["name"])
            ent_node = self._find_entity(nodes, entity_name)
            if ent_node:
                edges.append(self._edge(bean_nid, ent_node["id"], "manages",
                                        f"{bean['name']} manages {ent_node['name']}"))

        # Endpoint → service bean (heuristic: OrderController → OrderService)
        for ep in svc.get("endpoints", []):
            ep_nid = self._ep_id(project, ep["method"], ep["path"])
            ctrl = ep.get("controller", "")
            candidate = re.sub(r"Controller$", "Service", ctrl)
            svc_bean = self._find_bean(nodes, candidate, prefer_project=project)
            if svc_bean:
                edges.append(self._edge(ep_nid, svc_bean["id"], "handled_by",
                                        f"{ctrl} → {svc_bean['name']}"))

        # Entity → entity (JPA relationships)
        for ent in svc.get("entities", []):
            ent_nid = self._entity_id(project, ent["name"])
            for rel in ent.get("relationships", []):
                # Format: "OneToMany -> orderItems"
                parts = rel.split(" -> ", 1)
                if len(parts) != 2:
                    continue
                rel_type, field = parts
                # Convert field name to likely entity name: orderItems → OrderItem or Order
                target_name = field[0].upper() + field[1:]   # capitalise first letter
                # Try singular form too: orderItems → OrderItem
                if target_name.endswith("s"):
                    target_name = target_name[:-1]
                target = self._find_entity(nodes, target_name)
                if target:
                    edges.append(self._edge(ent_nid, target["id"],
                                            f"jpa_{rel_type.lower().replace('to', '_to_')}",
                                            f"{ent['name']} {rel_type} → {target['name']}"))

        # Kafka producer/consumer edges
        for topic in svc.get("events", {}).get("produces", []):
            topic_nid = f"kafka_topic::{topic}"
            # Link to any service bean in this project (pick first service bean)
            bean_node = self._find_any_service_bean(nodes, project)
            if bean_node:
                edges.append(self._edge(bean_node["id"], topic_nid, "produces_event",
                                        f"{bean_node['name']} produces {topic}"))

        for topic in svc.get("events", {}).get("consumes", []):
            topic_nid = f"kafka_topic::{topic}"
            bean_node = self._find_any_service_bean(nodes, project)
            if bean_node:
                edges.append(self._edge(topic_nid, bean_node["id"], "consumes_event",
                                        f"{bean_node['name']} consumes {topic}"))

        # Publisher REST endpoint → kafka_topic (endpoint publishes directly to Kafka)
        for kt in svc.get("kafka_topics", []):
            if kt.get("role") in ("producer", "both") and kt.get("publisher_endpoint"):
                topic_nid = f"kafka_topic::{kt['topic_name']}"
                # Find the matching endpoint node
                ep_str = kt["publisher_endpoint"]  # e.g. "POST /events/order"
                parts = ep_str.split(" ", 1)
                if len(parts) == 2:
                    ep_nid = self._find_endpoint_fuzzy(nodes, parts[0], parts[1], project)
                    if ep_nid and topic_nid in nodes:
                        edges.append(self._edge(
                            ep_nid, topic_nid, "publishes_to",
                            f"{ep_str} publishes to {kt['topic_name']}",
                        ))

        # Feign client → target endpoint
        for fc in svc.get("feign_clients", []):
            caller_bean = self._find_any_service_bean(nodes, project)
            target_project = fc.get("target_service", "")
            for call in fc.get("calls", []):
                parts = call.split(" ", 1)
                if len(parts) != 2:
                    continue
                method, path = parts
                target_ep_nid = self._ep_id(target_project, method, path)
                # Also try fuzzy path match
                if target_ep_nid not in nodes:
                    target_ep_nid = self._find_endpoint_fuzzy(nodes, method, path, target_project)
                if target_ep_nid and caller_bean:
                    edges.append(self._edge(caller_bean["id"], target_ep_nid, "feign_calls",
                                            f"{project} Feign→ {target_project} {method} {path}"))

        return edges

    def _build_angular_edges(self, nodes: dict, angular: dict,
                             spring_projects: list[str] | None = None) -> list[dict]:
        edges: list[dict] = []
        project = angular["project"]

        # Component → Angular service (from injected_services)
        for comp in angular.get("components", []):
            comp_nid = f"angular_component::{project}::{comp['name']}"
            for svc_type in comp.get("injected_services", []):
                svc_nid = f"angular_service::{project}::{svc_type}"
                if svc_nid in nodes:
                    edges.append(self._edge(comp_nid, svc_nid, "uses_service",
                                            f"{comp['name']} uses {svc_type}"))

        # Angular service → backend endpoint (from http_calls)
        connected_services: set[str] = set()
        for svc in angular.get("services", []):
            svc_nid = f"angular_service::{project}::{svc['name']}"
            for call in svc.get("http_calls", []):
                method = call.get("method", "GET")
                url = call.get("url", "")
                path = self._extract_path_from_url(url)
                if not path:
                    continue
                ep_nid = self._find_endpoint_fuzzy(nodes, method, path)
                if ep_nid:
                    edges.append(self._edge(svc_nid, ep_nid, "http_call",
                                            f"{svc['name']}.{method.lower()}() → {path}"))
                    connected_services.add(svc_nid)

        # Naming-convention fallback: Angular service → backend service bean
        if spring_projects:
            for svc in angular.get("services", []):
                svc_nid = f"angular_service::{project}::{svc['name']}"
                if svc_nid in connected_services:
                    continue
                target_project = _infer_backend_project(svc["name"], spring_projects)
                if target_project:
                    target_bean = self._find_any_service_bean(nodes, target_project)
                    if target_bean:
                        edges.append(self._edge(
                            svc_nid, target_bean["id"], "inferred_http_call",
                            f"{svc['name']} → {target_project} (inferred by name)",
                        ))

        return edges

    def _build_master_edges(self, nodes: dict, master: dict) -> list[dict]:
        edges: list[dict] = []

        for contract in master.get("api_contracts", []):
            caller_project = contract.get("caller", "")
            target_project = contract.get("service", "")
            ep_str = contract.get("endpoint", "")
            ang_svc_str = contract.get("angular_service", "")

            parts = ep_str.split(" ", 1)
            if len(parts) != 2:
                continue
            method, path = parts

            # Angular service name: "OrderService.get" → "OrderService"
            ang_svc_name = ang_svc_str.split(".")[0] if "." in ang_svc_str else ang_svc_str
            ang_nid = f"angular_service::{caller_project}::{ang_svc_name}"
            ep_nid = self._ep_id(target_project, method, path)
            if ep_nid not in nodes:
                ep_nid = self._find_endpoint_fuzzy(nodes, method, path, target_project)

            if ang_nid in nodes and ep_nid:
                edges.append(self._edge(ang_nid, ep_nid, "http_call",
                                        f"{ang_svc_str} → {target_project} {ep_str}"))

        return edges

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def _find_bean(self, nodes: dict, name: str, prefer_project: str = "") -> dict | None:
        # Exact match
        nid = self._bean_id(prefer_project, name)
        if nid in nodes:
            return nodes[nid]
        # Search all projects
        for node in nodes.values():
            if node.get("name") == name and node["type"].startswith("spring_"):
                return node
        # Suffix-stripped match (OrderServiceImpl → OrderService)
        name_stripped = re.sub(r"(Impl|Service|Repository|Repo)$", "", name)
        if name_stripped and name_stripped != name:
            for node in nodes.values():
                if not node["type"].startswith("spring_"):
                    continue
                node_stripped = re.sub(r"(Impl|Service|Repository|Repo)$", "", node.get("name", ""))
                if node_stripped == name_stripped:
                    return node
        return None

    def _find_entity(self, nodes: dict, name: str, prefer_project: str = "") -> dict | None:
        nid = self._entity_id(prefer_project, name)
        if nid in nodes:
            return nodes[nid]
        for node in nodes.values():
            if node.get("type") == "entity" and node.get("name", "").lower() == name.lower():
                return node
        return None

    def _find_any_service_bean(self, nodes: dict, project: str) -> dict | None:
        for node in nodes.values():
            if node.get("project") == project and node.get("type") == "spring_service":
                return node
        # Fall back to any bean in the project
        for node in nodes.values():
            if node.get("project") == project and node["type"].startswith("spring_"):
                return node
        return None

    def _find_endpoint_fuzzy(self, nodes: dict, method: str, path: str,
                             prefer_project: str = "") -> str | None:
        norm_path = self._norm_path(path)

        def _matches(node_path: str) -> bool:
            """Exact match or suffix match to handle prefix differences like /private_api/."""
            np = self._norm_path(node_path)
            if np == norm_path:
                return True
            # Allow one side to be a suffix of the other (strips versioning prefixes)
            if np.endswith(norm_path) or norm_path.endswith(np):
                return True
            return False

        # Prefer matching project first
        for nid, node in nodes.items():
            if node.get("type") != "endpoint":
                continue
            if prefer_project and node.get("project") != prefer_project:
                continue
            if node.get("method") == method and _matches(node.get("path", "")):
                return nid
        # Any project
        for nid, node in nodes.items():
            if node.get("type") != "endpoint":
                continue
            if node.get("method") == method and _matches(node.get("path", "")):
                return nid
        return None

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _ep_id(project: str, method: str, path: str) -> str:
        return f"endpoint::{project}::{method}::{path}"

    @staticmethod
    def _bean_id(project: str, name: str) -> str:
        return f"bean::{project}::{name}"

    @staticmethod
    def _entity_id(project: str, name: str) -> str:
        return f"entity::{project}::{name}"

    @staticmethod
    def _edge(from_id: str, to_id: str, edge_type: str, label: str = "") -> dict:
        return {"from": from_id, "to": to_id, "type": edge_type, "label": label}

    @staticmethod
    def _norm_path(path: str) -> str:
        """Normalize path for comparison: replace {variable} and trailing slash."""
        return re.sub(r"\{[^}]+\}", "*", path).rstrip("/") or "/"

    @staticmethod
    def _extract_path_from_url(url: str) -> str:
        """Extract the path portion from an Angular service URL expression."""
        url = url.strip("'\"` ")
        # Full URL with protocol
        m = re.search(r"https?://[^/\s'\"]+(/[^?#'\"\s]*)", url)
        if m:
            return m.group(1)
        # Constructed: baseUrl + '/orders' or `${env}/orders`
        parts = re.findall(r"['\"`]([^'\"` ]+)['\"`]", url)
        for part in parts:
            if part.startswith("/"):
                return part
        # Already a path
        if url.startswith("/"):
            return url
        return ""

    @staticmethod
    def _count_types(items, key: str) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for item in items:
            counts[item.get(key, "unknown")] += 1
        return dict(counts)
