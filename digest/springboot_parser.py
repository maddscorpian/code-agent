from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .models import (
    EndpointDigest,
    EntityDigest,
    EventDigest,
    FeignClientDigest,
    ServiceDigest,
)


HTTP_ANN_TO_METHOD = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "GET",
}


class SpringBootParser:
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)

    def parse(self) -> ServiceDigest:
        project = self.project_path.name
        java_files = list(self.project_path.rglob("*.java"))
        endpoints: list[EndpointDigest] = []
        entities: list[EntityDigest] = []
        feign_clients: list[FeignClientDigest] = []
        dtos: list[str] = []
        consumes: set[str] = set()
        produces: set[str] = set()
        security_config: dict = {}

        for file in java_files:
            text = self._safe_read(file)
            if not text:
                continue
            endpoints.extend(self._parse_controllers(text))
            entities.extend(self._parse_entities(text))
            feign_clients.extend(self._parse_feign(text))
            dtos.extend(self._parse_dtos(text))
            c, p = self._parse_events(text)
            consumes.update(c)
            produces.update(p)
            if "SecurityFilterChain" in text or "WebSecurityConfigurerAdapter" in text:
                security_config.update(self._parse_security(text))

        security_config.update(self._parse_application_config())
        return ServiceDigest(
            project=project,
            type="spring-boot",
            created_at=self._now(),
            endpoints=endpoints,
            entities=entities,
            dtos=sorted(set(dtos)),
            feign_clients=feign_clients,
            events=EventDigest(produces=sorted(produces), consumes=sorted(consumes)),
            security_config=security_config,
        )

    def _parse_controllers(self, text: str) -> list[EndpointDigest]:
        if "@RestController" not in text and "@Controller" not in text:
            return []
        class_base = self._annotation_value(text, r"@RequestMapping\((.*?)\)") or ""
        controller_name = self._class_name(text) or "UnknownController"
        rows: list[EndpointDigest] = []

        method_pattern = re.compile(
            r"(@(?:GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\((.*?)\)[\s\S]*?)(public\s+([A-Za-z0-9_<>, ?]+)\s+([A-Za-z0-9_]+)\s*\((.*?)\))",
            re.MULTILINE,
        )
        for m in method_pattern.finditer(text):
            ann_block, ann_args, _, response_type, handler, params = m.groups()
            ann_name = re.search(r"@([A-Za-z]+)\(", ann_block)
            ann = ann_name.group(1) if ann_name else "RequestMapping"
            http_method = HTTP_ANN_TO_METHOD.get(ann, "GET")
            if ann == "RequestMapping":
                req_method = re.search(r"RequestMethod\.([A-Z]+)", ann_block)
                if req_method:
                    http_method = req_method.group(1)
            method_path = self._extract_path_from_mapping_args(ann_args)
            full_path = self._join_paths(class_base, method_path)
            request_dto = self._extract_request_body_type(params)
            roles = self._extract_roles(ann_block)
            auth_required = bool(roles) or "@PreAuthorize" in ann_block or "@Secured" in ann_block
            rows.append(
                EndpointDigest(
                    path=full_path,
                    method=http_method,
                    controller=controller_name,
                    handler=handler,
                    request_dto=request_dto,
                    response_dto=response_type.strip(),
                    auth_required=auth_required,
                    roles=roles,
                )
            )
        return rows

    def _parse_entities(self, text: str) -> list[EntityDigest]:
        if "@Entity" not in text:
            return []
        name = self._class_name(text) or "UnknownEntity"
        table = self._annotation_value(text, r"@Table\((.*?)\)") or name
        fields = re.findall(r"@Column(?:\([^)]*\))?\s+private\s+[A-Za-z0-9_<>, ?]+\s+([A-Za-z0-9_]+)\s*;", text)
        relationships: list[str] = []
        rel_map = {"OneToMany": "OneToMany", "ManyToOne": "ManyToOne", "ManyToMany": "ManyToMany"}
        for ann, label in rel_map.items():
            for target in re.findall(rf"@{ann}[\s\S]*?private\s+[A-Za-z0-9_<>, ?]+\s+([A-Za-z0-9_]+)\s*;", text):
                relationships.append(f"{label} -> {target}")
        return [EntityDigest(name=name, table=self._extract_name_arg(table), fields=fields, relationships=relationships)]

    def _parse_dtos(self, text: str) -> list[str]:
        classes = re.findall(r"class\s+([A-Za-z0-9_]+)", text)
        return [c for c in classes if any(token in c for token in ("DTO", "Request", "Response"))]

    def _parse_feign(self, text: str) -> list[FeignClientDigest]:
        if "@FeignClient" not in text:
            return []
        raw = self._annotation_value(text, r"@FeignClient\((.*?)\)") or ""
        client_name = self._extract_named_arg(raw, "name") or self._extract_named_arg(raw, "value") or "unknown"
        target = self._extract_named_arg(raw, "contextId") or client_name
        calls: list[str] = []
        for ann, args in re.findall(r"@((?:Get|Post|Put|Delete|Patch)Mapping|RequestMapping)\((.*?)\)", text):
            path = self._extract_path_from_mapping_args(args)
            method = HTTP_ANN_TO_METHOD.get(ann, "GET")
            if ann == "RequestMapping":
                explicit = re.search(r"RequestMethod\.([A-Z]+)", args)
                if explicit:
                    method = explicit.group(1)
            calls.append(f"{method} {path}")
        return [FeignClientDigest(client_name=client_name, target_service=target, calls=calls)]

    def _parse_events(self, text: str) -> tuple[set[str], set[str]]:
        consumes = set(re.findall(r'@KafkaListener\([^)]*topics\s*=\s*"?([A-Za-z0-9._-]+)"?', text))
        consumes.update(re.findall(r'@RabbitListener\([^)]*queues\s*=\s*"?([A-Za-z0-9._-]+)"?', text))
        produces = set(re.findall(r'KafkaTemplate\.[^(]*\(\s*"([A-Za-z0-9._-]+)"', text))
        produces.update(re.findall(r'RabbitTemplate\.[^(]*\(\s*"([A-Za-z0-9._-]+)"', text))
        return consumes, produces

    def _parse_security(self, text: str) -> dict:
        return {
            "jwt_filter_present": "JwtAuthenticationFilter" in text or "JwtTokenProvider" in text,
            "cors_configured": "cors(" in text.lower() or "CorsConfiguration" in text,
            "permit_all_paths": re.findall(r'requestMatchers\("([^"]+)"\)\.permitAll\(\)', text),
            "authenticated_paths": re.findall(r'requestMatchers\("([^"]+)"\)\.authenticated\(\)', text),
        }

    def _parse_application_config(self) -> dict:
        data = {}
        for file in self._iter_config_files():
            text = self._safe_read(file)
            if not text:
                continue
            for key in ("server.port", "spring.application.name"):
                m = re.search(rf"{re.escape(key)}\s*[:=]\s*(.+)", text)
                if m:
                    data[key] = m.group(1).strip()
            if "spring.datasource" in text:
                data["has_datasource"] = True
            if "feign" in text.lower():
                data["has_feign"] = True
            if "eureka" in text.lower():
                data["has_eureka"] = True
        return data

    def _iter_config_files(self) -> Iterable[Path]:
        yield from self.project_path.rglob("application.yml")
        yield from self.project_path.rglob("application.yaml")
        yield from self.project_path.rglob("application.properties")

    @staticmethod
    def _annotation_value(text: str, pattern: str) -> str | None:
        m = re.search(pattern, text, re.MULTILINE)
        return m.group(1) if m else None

    @staticmethod
    def _class_name(text: str) -> str | None:
        m = re.search(r"class\s+([A-Za-z0-9_]+)", text)
        return m.group(1) if m else None

    @staticmethod
    def _extract_named_arg(raw: str, key: str) -> str | None:
        m = re.search(rf'{key}\s*=\s*"([^"]+)"', raw)
        return m.group(1) if m else None

    @staticmethod
    def _extract_name_arg(raw: str) -> str:
        m = re.search(r'name\s*=\s*"([^"]+)"', raw)
        if m:
            return m.group(1)
        return raw.strip('"')

    @staticmethod
    def _extract_path_from_mapping_args(args: str) -> str:
        key_match = re.search(r'(?:path|value)\s*=\s*"([^"]+)"', args)
        if key_match:
            return key_match.group(1)
        quote = re.search(r'"([^"]+)"', args)
        return quote.group(1) if quote else ""

    @staticmethod
    def _extract_request_body_type(params: str) -> str | None:
        m = re.search(r"@RequestBody\s+([A-Za-z0-9_<>]+)", params)
        return m.group(1) if m else None

    @staticmethod
    def _extract_roles(text: str) -> list[str]:
        roles = re.findall(r"ROLE_[A-Z_]+", text)
        return sorted(set(roles))

    @staticmethod
    def _join_paths(a: str, b: str) -> str:
        left = (a or "").strip().strip('"')
        right = (b or "").strip().strip('"')
        base = "/" + left.strip("/") if left else ""
        tail = "/" + right.strip("/") if right else ""
        full = (base + tail) or "/"
        return re.sub(r"//+", "/", full)

    @staticmethod
    def _safe_read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()
