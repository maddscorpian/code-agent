from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path


class ApiCatalogBuilder:
    """
    Builds a consolidated API catalog from all digest files.

    Outputs:
      api-catalog/openapi.json    — OpenAPI 3.0.3 spec (Swagger-compatible)
      api-catalog/api-catalog.md  — Markdown summary table
    """

    def __init__(self, digests_dir: str, output_dir: str):
        self.digests_dir = Path(digests_dir)
        self.output_dir = Path(output_dir)

    def build(self) -> dict:
        """Build and write both artifacts. Returns the OpenAPI dict."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        service_digests, angular_digest, _ = self._load_digests()

        openapi = self._build_openapi(service_digests)
        (self.output_dir / "openapi.json").write_text(
            json.dumps(openapi, indent=2), encoding="utf-8"
        )

        md = self._build_markdown(service_digests)
        (self.output_dir / "api-catalog.md").write_text(md, encoding="utf-8")

        return openapi

    # ------------------------------------------------------------------
    # Digest loading
    # ------------------------------------------------------------------

    def _load_digests(self) -> tuple[list[dict], dict | None, dict | None]:
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
    # OpenAPI 3.0.3
    # ------------------------------------------------------------------

    def _build_openapi(self, services: list[dict]) -> dict:
        paths: dict = {}
        schemas: dict = {}
        tags: list[dict] = []
        total = 0

        for svc in sorted(services, key=lambda s: s["project"]):
            project = svc["project"]
            eps = svc.get("endpoints", [])
            feigns = svc.get("feign_clients", [])

            tag_desc = f"{project} — {len(eps)} endpoints"
            if feigns:
                downstream = ", ".join(
                    fc.get("target_service", fc.get("client_name", "?"))
                    for fc in feigns
                )
                tag_desc += f" | downstream: {downstream}"
            tags.append({"name": project, "description": tag_desc})

            for ep in eps:
                path = ep.get("path") or "/"
                method = ep.get("method", "GET").lower()

                # OpenAPI path params: /api/orders/{id}
                if path not in paths:
                    paths[path] = {}

                # Path-level parameters from {variable} segments
                param_names = re.findall(r"\{([^}]+)\}", path)
                params = [
                    {
                        "name": p,
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                    for p in param_names
                ]

                operation: dict = {
                    "tags": [project],
                    "operationId": f"{project}_{ep.get('handler', 'unknown')}_{method}",
                    "summary": ep.get("javadoc") or f"{ep.get('method','GET')} {path}",
                    "parameters": params,
                    "responses": {"200": {"description": "Success"}},
                }

                if ep.get("auth_required"):
                    operation["security"] = [{"bearerAuth": []}]
                if ep.get("roles"):
                    operation["x-roles"] = ep["roles"]

                # Request body
                req_dto = ep.get("request_dto")
                if req_dto and method in ("post", "put", "patch"):
                    schemas.setdefault(req_dto, {"type": "object", "x-dto": req_dto})
                    operation["requestBody"] = {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": f"#/components/schemas/{req_dto}"}
                            }
                        },
                    }

                # Response body — unwrap ResponseEntity<Foo> / List<Foo>
                resp_dto = ep.get("response_dto", "")
                if resp_dto:
                    inner = re.sub(r"^ResponseEntity<(.+)>$", r"\1", resp_dto.strip())
                    inner = re.sub(r"^List<(.+)>$", r"\1", inner).strip()
                    if inner and inner not in {"Void", "void", "String", "Object", "?"}:
                        schemas.setdefault(inner, {"type": "object", "x-dto": inner})
                        operation["responses"]["200"] = {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": f"#/components/schemas/{inner}"}
                                }
                            },
                        }

                paths[path][method] = operation
                total += 1

            # Feign clients as x-extension on tag
            if feigns:
                feign_info = []
                for fc in feigns:
                    feign_info.append({
                        "client": fc["client_name"],
                        "target": fc.get("target_service", ""),
                        "url": fc.get("resolved_url", ""),
                        "url_property": fc.get("url_property_key", ""),
                        "calls": fc.get("calls", []),
                    })
                tags[-1]["x-feign-clients"] = feign_info

        # Entity schemas
        for svc in services:
            for ent in svc.get("entities", []):
                name = ent["name"]
                if name not in schemas:
                    schemas[name] = {
                        "type": "object",
                        "x-table": ent.get("table", ""),
                        "properties": {
                            f: {"type": "string"} for f in ent.get("fields", [])
                        },
                    }

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "openapi": "3.0.3",
            "info": {
                "title": "Platform API Catalog",
                "description": "Auto-generated from codebase digests. Do not edit manually.",
                "version": datetime.now(timezone.utc).strftime("%Y%m%d"),
                "x-generated-at": now,
                "x-total-endpoints": total,
            },
            "tags": tags,
            "paths": paths,
            "components": {
                "schemas": schemas,
                "securitySchemes": {
                    "bearerAuth": {
                        "type": "http",
                        "scheme": "bearer",
                        "bearerFormat": "JWT",
                    }
                },
            },
        }

    # ------------------------------------------------------------------
    # Markdown summary
    # ------------------------------------------------------------------

    def _build_markdown(self, services: list[dict]) -> str:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        total = sum(len(s.get("endpoints", [])) for s in services)
        lines = [
            "# API Catalog",
            "",
            f"Generated: {now}  ",
            f"Total endpoints: **{total}** across **{len(services)}** services",
            "",
        ]

        for svc in sorted(services, key=lambda s: s["project"]):
            project = svc["project"]
            eps = svc.get("endpoints", [])
            feigns = svc.get("feign_clients", [])
            if not eps and not feigns:
                continue

            lines += [f"## {project}", ""]

            if eps:
                lines += [
                    f"### Endpoints ({len(eps)})",
                    "",
                    "| Method | Path | Auth | Roles | Handler | Request DTO |",
                    "|--------|------|:----:|-------|---------|-------------|",
                ]
                for ep in sorted(eps, key=lambda e: (e.get("path", ""), e.get("method", ""))):
                    auth = "✓" if ep.get("auth_required") else ""
                    roles = ", ".join(ep.get("roles", [])) or "—"
                    req_dto = ep.get("request_dto") or "—"
                    handler = ep.get("handler", "—")
                    lines.append(
                        f"| `{ep.get('method','')}` | `{ep.get('path','')}` "
                        f"| {auth} | {roles} | `{handler}` | `{req_dto}` |"
                    )
                lines.append("")

            if feigns:
                lines += [
                    f"### Downstream Feign Clients ({len(feigns)})",
                    "",
                    "| Client | Target Service | Resolved URL | Property Key | Calls |",
                    "|--------|---------------|-------------|-------------|-------|",
                ]
                for fc in feigns:
                    url = fc.get("resolved_url", "") or "—"
                    prop = fc.get("url_property_key", "") or "—"
                    target = fc.get("target_service", "—")
                    calls = " · ".join(fc.get("calls", [])[:6]) or "—"
                    lines.append(
                        f"| `{fc['client_name']}` | `{target}` | `{url}` | `{prop}` | {calls} |"
                    )
                lines.append("")

        return "\n".join(lines)
