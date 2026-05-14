from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath

_SHARED_DIRS = {
    "shared", "common", "core", "layout", "header", "footer", "nav",
    "util", "utils", "helpers", "directives", "pipes", "guards", "models",
    "interfaces", "services", "store", "ngrx", "state", "interceptors",
    "abstract", "base", "generic", "lib",
}

_FEATURE_CONTAINERS = {"components", "pages", "containers", "views", "features", "modules"}


def _detect_feature_from_path(file_path: str) -> str | None:
    """
    Extract a feature slug from a component file path.
    Looks for the first directory under a known feature container that is not a shared directory.
    E.g. 'src/app/components/book-appointment-slot/foo.component.ts' → 'book-appointment-slot'
    """
    parts = PurePosixPath(file_path).parts
    for i, part in enumerate(parts):
        if part in _FEATURE_CONTAINERS:
            if i + 1 < len(parts):
                candidate = parts[i + 1]
                if candidate not in _SHARED_DIRS and not candidate.endswith(".ts"):
                    return candidate
    return None


def _slug_to_title(slug: str) -> str:
    """'book-appointment-slot' → 'Book Appointment Slot'"""
    return " ".join(w.capitalize() for w in slug.split("-"))


def _infer_backend_project(angular_service_name: str, spring_projects: list[str]) -> str | None:
    """
    Match an Angular service name to a Spring Boot project by naming convention.
    Strips common suffixes, converts to kebab-case, then checks for substring match
    against project names (also tries singular form).
    """
    _EXPLICIT_ALIASES: dict[str, str] = {
        "auth": "ms-java-identity",
        "irp": "ms-java-identity",
        "security": "ms-java-security",
    }
    name_lower = angular_service_name.lower()

    for keyword, proj in _EXPLICIT_ALIASES.items():
        if keyword in name_lower and proj in spring_projects:
            return proj

    for proj in spring_projects:
        proj_core = proj.replace("ms-java-", "").replace("module-java-", "")
        cores = [proj_core]
        if proj_core.endswith("s"):
            cores.append(proj_core[:-1])   # singular
        for core in cores:
            if len(core) >= 4 and core in name_lower:
                return proj

    return None


class FeatureGraphBuilder:
    """
    Detects user-facing features from an Angular digest and creates:
    - user_function nodes (one per detected feature)
    - part_of_feature edges  (angular_component → user_function)
    - feature_uses edges     (user_function → angular_service)
    - feature_calls edges    (user_function → spring_service, via name convention)
    """

    def __init__(self, angular_digest: dict, nodes: dict, spring_projects: list[str]):
        self.digest = angular_digest
        self.nodes = nodes
        self.spring_projects = spring_projects
        self.project = angular_digest.get("project", "unknown")

    def build(self) -> tuple[dict[str, dict], list[dict]]:
        new_nodes: dict[str, dict] = {}
        new_edges: list[dict] = []

        # Group components by feature slug
        feature_components: dict[str, list[str]] = defaultdict(list)
        component_services: dict[str, list[str]] = {}

        for comp in self.digest.get("components", []):
            file_path = comp.get("file_path", "")
            feature = _detect_feature_from_path(file_path)
            if feature:
                feature_components[feature].append(comp["name"])
                component_services[comp["name"]] = comp.get("injected_services", [])

        for feature_key, comp_names in feature_components.items():
            feature_name = _slug_to_title(feature_key)
            nid = f"user_function::{self.project}::{feature_key}"

            # Collect angular services used by ALL components in this feature
            angular_services: set[str] = set()
            for comp_name in comp_names:
                angular_services.update(component_services.get(comp_name, []))

            # Infer backend projects from those service names
            backend_projects: set[str] = set()
            for svc_name in angular_services:
                proj = _infer_backend_project(svc_name, self.spring_projects)
                if proj:
                    backend_projects.add(proj)

            # Heuristic: entry components share the feature slug in their name
            feature_slug_compact = feature_key.replace("-", "")
            entry_components = [
                c for c in comp_names
                if feature_slug_compact in c.lower().replace("component", "")
            ] or comp_names[:1]

            new_nodes[nid] = {
                "id": nid,
                "type": "user_function",
                "project": self.project,
                "name": feature_name,
                "feature_key": feature_key,
                "entry_components": entry_components,
                "angular_services": sorted(angular_services),
                "backend_projects": sorted(backend_projects),
                "label": f"User Function: {feature_name} [{self.project}]",
            }

            # part_of_feature: component → user_function
            for comp_name in comp_names:
                comp_nid = f"angular_component::{self.project}::{comp_name}"
                if comp_nid in self.nodes:
                    new_edges.append({
                        "from": comp_nid,
                        "to": nid,
                        "type": "part_of_feature",
                        "label": f"{comp_name} is part of {feature_name}",
                    })

            # feature_uses: user_function → angular_service
            for svc_name in angular_services:
                svc_nid = f"angular_service::{self.project}::{svc_name}"
                if svc_nid in self.nodes:
                    new_edges.append({
                        "from": nid,
                        "to": svc_nid,
                        "type": "feature_uses",
                        "label": f"{feature_name} uses {svc_name}",
                    })

            # feature_calls: user_function → spring_service (one per backend project)
            for backend_proj in backend_projects:
                added = False
                for node_id, node in self.nodes.items():
                    if node.get("project") == backend_proj and node.get("type") == "spring_service":
                        new_edges.append({
                            "from": nid,
                            "to": node_id,
                            "type": "feature_calls",
                            "label": f"{feature_name} calls {node.get('name', '')} [{backend_proj}]",
                        })
                        if not added:
                            added = True   # keep going — add all spring_service nodes in this project

        return new_nodes, new_edges
