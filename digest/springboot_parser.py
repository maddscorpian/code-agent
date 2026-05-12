from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .models import (
    BeanDigest,
    EndpointDigest,
    EntityDigest,
    EventDigest,
    ExceptionHandlerDigest,
    FeignClientDigest,
    ScheduledTaskDigest,
    ServiceDigest,
)
from .pom_parser import PomParser

HTTP_ANN_TO_METHOD = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "GET",
}

BEAN_ANNOTATIONS = {
    "Service": "service",
    "Repository": "repository",
    "Component": "component",
    "Configuration": "configuration",
    "ControllerAdvice": "advice",
    "RestControllerAdvice": "advice",
}

CONTROLLER_ANNOTATIONS = {"RestController", "Controller"}


class SpringBootParser:
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)
        self._pom = PomParser()

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
        beans: list[BeanDigest] = []
        exception_handlers: list[ExceptionHandlerDigest] = []
        scheduled_tasks: list[ScheduledTaskDigest] = []

        for file in java_files:
            text = self._safe_read(file)
            if not text:
                continue
            rel = str(file.relative_to(self.project_path))

            ast_info = self._parse_with_ast(text, rel)
            if ast_info:
                bean_type = ast_info.get("bean_type")
                class_name = ast_info.get("class_name", "Unknown")

                if bean_type == "controller" or (ast_info.get("has_controller") and not bean_type):
                    endpoints.extend(self._parse_controllers(text))
                elif bean_type in BEAN_ANNOTATIONS.values() and bean_type != "advice":
                    beans.append(
                        BeanDigest(
                            name=class_name,
                            bean_type=bean_type,
                            file_path=rel,
                            dependencies=ast_info.get("dependencies", []),
                            methods=ast_info.get("method_names", []),
                            transactional_methods=ast_info.get("transactional_methods", []),
                        )
                    )
                elif bean_type == "advice":
                    exception_handlers.append(
                        ExceptionHandlerDigest(
                            advice_class=class_name,
                            handled_exceptions=ast_info.get("handled_exceptions", []),
                        )
                    )

                for sched in ast_info.get("scheduled_methods", []):
                    scheduled_tasks.append(
                        ScheduledTaskDigest(
                            class_name=class_name,
                            method=sched["name"],
                            schedule=sched["schedule"],
                        )
                    )

                if ast_info.get("has_entity"):
                    entities.extend(self._parse_entities(text))
                if ast_info.get("has_feign"):
                    feign_clients.extend(self._parse_feign(text))
            else:
                # Regex fallback for files javalang can't parse
                endpoints.extend(self._parse_controllers(text))
                entities.extend(self._parse_entities(text))
                feign_clients.extend(self._parse_feign(text))

            dtos.extend(self._parse_dtos(text))
            c, p = self._parse_events(text)
            consumes.update(c)
            produces.update(p)
            if "SecurityFilterChain" in text or "WebSecurityConfigurerAdapter" in text:
                security_config.update(self._parse_security(text))

        # Entities may be missed if AST succeeded but didn't flag has_entity — run
        # a regex pass over all files to catch any remaining @Entity classes.
        if not entities:
            for file in java_files:
                text = self._safe_read(file)
                if text and "@Entity" in text:
                    entities.extend(self._parse_entities(text))

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
            beans=beans,
            exception_handlers=exception_handlers,
            scheduled_tasks=scheduled_tasks,
            build_dependencies=self._pom.parse_dependencies(str(self.project_path)),
            db_migrations=self._pom.parse_migrations(str(self.project_path)),
        )

    # ------------------------------------------------------------------
    # AST-based structural parsing
    # ------------------------------------------------------------------

    def _parse_with_ast(self, source: str, rel_path: str) -> dict | None:
        """Parse Java file with javalang. Returns structural info or None on failure."""
        try:
            import javalang
            tree = javalang.parse.parse(source)
        except Exception:
            return None

        result: dict = {
            "class_name": None,
            "bean_type": None,
            "has_controller": False,
            "has_entity": False,
            "has_feign": False,
            "dependencies": [],
            "method_names": [],
            "transactional_methods": [],
            "scheduled_methods": [],
            "handled_exceptions": [],
        }

        try:
            import javalang

            # Class declarations
            for _, cls_node in tree.filter(javalang.tree.ClassDeclaration):
                if result["class_name"]:
                    break  # use first top-level class
                result["class_name"] = cls_node.name
                ann_names = {a.name for a in (cls_node.annotations or [])}

                if ann_names & CONTROLLER_ANNOTATIONS:
                    result["bean_type"] = "controller"
                    result["has_controller"] = True
                else:
                    for ann, btype in BEAN_ANNOTATIONS.items():
                        if ann in ann_names:
                            result["bean_type"] = btype
                            break

                if "Entity" in ann_names:
                    result["has_entity"] = True
                if "FeignClient" in ann_names:
                    result["has_feign"] = True

                # Constructor injection
                for ctor in (cls_node.constructors or []):
                    for param in (ctor.parameters or []):
                        if hasattr(param.type, "name"):
                            result["dependencies"].append(param.type.name)

                # @Autowired field injection
                for field in (cls_node.fields or []):
                    field_anns = {a.name for a in (field.annotations or [])}
                    if "Autowired" in field_anns and hasattr(field.type, "name"):
                        result["dependencies"].append(field.type.name)

                # Methods
                for method in (cls_node.methods or []):
                    result["method_names"].append(method.name)
                    method_anns = {a.name for a in (method.annotations or [])}

                    if "Transactional" in method_anns:
                        result["transactional_methods"].append(method.name)

                    if "Scheduled" in method_anns:
                        for ann in (method.annotations or []):
                            if ann.name == "Scheduled":
                                schedule_val = self._extract_annotation_string_value(ann)
                                result["scheduled_methods"].append(
                                    {"name": method.name, "schedule": schedule_val}
                                )

                    if "ExceptionHandler" in method_anns:
                        for param in (method.parameters or []):
                            if hasattr(param.type, "name"):
                                result["handled_exceptions"].append(param.type.name)

            # Interface declarations (Repository interfaces)
            for _, iface_node in tree.filter(javalang.tree.InterfaceDeclaration):
                if result["class_name"]:
                    break
                result["class_name"] = iface_node.name
                ann_names = {a.name for a in (iface_node.annotations or [])}
                if ann_names & {"Repository", "RepositoryRestResource"}:
                    result["bean_type"] = "repository"
                for method in (iface_node.methods or []):
                    result["method_names"].append(method.name)

        except Exception:
            return None

        return result if result["class_name"] else None

    @staticmethod
    def _extract_annotation_string_value(ann) -> str:
        """Extract first string-like value from an annotation (for @Scheduled cron/fixedRate)."""
        try:
            if ann.element is None:
                return "unknown"
            if isinstance(ann.element, list):
                for pair in ann.element:
                    if hasattr(pair, "name") and hasattr(pair, "value"):
                        val = pair.value
                        raw = getattr(val, "value", str(val))
                        return f"{pair.name}={raw}"
            val = ann.element
            raw = getattr(val, "value", str(val))
            return str(raw)
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------
    # Regex-based extraction (kept from v1, used for annotation values)
    # ------------------------------------------------------------------

    def _parse_controllers(self, text: str) -> list[EndpointDigest]:
        if "@RestController" not in text and "@Controller" not in text:
            return []
        class_base = self._annotation_value(text, r"@RequestMapping\((.*?)\)") or ""
        controller_name = self._class_name(text) or "UnknownController"
        rows: list[EndpointDigest] = []

        method_pattern = re.compile(
            r"(@(?:GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\((.*?)\)[\s\S]*?)"
            r"(public\s+([A-Za-z0-9_<>, ?]+)\s+([A-Za-z0-9_]+)\s*\((.*?)\))",
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
            javadoc = self._extract_javadoc_before(text, ann_block[:30])
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
                    javadoc=javadoc,
                )
            )
        return rows

    def _parse_entities(self, text: str) -> list[EntityDigest]:
        if "@Entity" not in text:
            return []
        name = self._class_name(text) or "UnknownEntity"
        table = self._annotation_value(text, r"@Table\((.*?)\)") or name
        fields = re.findall(
            r"@Column(?:\([^)]*\))?\s+private\s+[A-Za-z0-9_<>, ?]+\s+([A-Za-z0-9_]+)\s*;", text
        )
        # Also catch fields without @Column
        id_fields = re.findall(r"@Id\s+(?:@[^\n]+\n\s*)?private\s+[A-Za-z0-9_<>]+\s+(\w+)\s*;", text)
        all_fields = list(dict.fromkeys(id_fields + fields))
        relationships: list[str] = []
        for ann, label in {"OneToMany": "OneToMany", "ManyToOne": "ManyToOne", "ManyToMany": "ManyToMany", "OneToOne": "OneToOne"}.items():
            for target in re.findall(
                rf"@{ann}[\s\S]*?private\s+[A-Za-z0-9_<>, ?]+\s+([A-Za-z0-9_]+)\s*;", text
            ):
                relationships.append(f"{label} -> {target}")
        return [EntityDigest(name=name, table=self._extract_name_arg(table), fields=all_fields, relationships=relationships)]

    def _parse_dtos(self, text: str) -> list[str]:
        classes = re.findall(r"class\s+([A-Za-z0-9_]+)", text)
        return [c for c in classes if any(token in c for token in ("DTO", "Dto", "Request", "Response", "Payload"))]

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
        produces = set(re.findall(r'kafkaTemplate\.[^(]*\(\s*"([A-Za-z0-9._-]+)"', text))
        produces.update(re.findall(r'rabbitTemplate\.[^(]*\(\s*"([A-Za-z0-9._-]+)"', text))
        # Also handle KafkaTemplate.send("topic", ...)
        produces.update(re.findall(r'\.send\(\s*"([A-Za-z0-9._-]+)"', text))
        return consumes, produces

    def _parse_security(self, text: str) -> dict:
        return {
            "jwt_filter_present": "JwtAuthenticationFilter" in text or "JwtTokenProvider" in text or "JwtUtil" in text,
            "cors_configured": "cors(" in text.lower() or "CorsConfiguration" in text,
            "permit_all_paths": re.findall(r'requestMatchers\("([^"]+)"\)\.permitAll\(\)', text),
            "authenticated_paths": re.findall(r'requestMatchers\("([^"]+)"\)\.authenticated\(\)', text),
            "oauth2_enabled": "oauth2" in text.lower() or "OAuth2" in text,
        }

    def _parse_application_config(self) -> dict:
        data: dict = {}
        for file in self._iter_config_files():
            text = self._safe_read(file)
            if not text:
                continue
            for key in ("server.port", "spring.application.name", "spring.datasource.url"):
                m = re.search(rf"{re.escape(key)}\s*[:=]\s*(.+)", text)
                if m:
                    data[key] = m.group(1).strip()
            if "spring.datasource" in text:
                data["has_datasource"] = True
            if "feign" in text.lower():
                data["has_feign"] = True
            if "eureka" in text.lower():
                data["has_eureka"] = True
            if "kafka" in text.lower():
                data["has_kafka"] = True
            if "rabbitmq" in text.lower():
                data["has_rabbitmq"] = True
            # Active profiles
            profiles = re.findall(r"spring\.profiles\.active\s*[:=]\s*(.+)", text)
            if profiles:
                data["active_profiles"] = [p.strip() for p in profiles[-1].split(",")]
        return data

    def _iter_config_files(self) -> Iterable[Path]:
        yield from self.project_path.rglob("application.yml")
        yield from self.project_path.rglob("application.yaml")
        yield from self.project_path.rglob("application.properties")
        yield from self.project_path.rglob("application-*.yml")
        yield from self.project_path.rglob("application-*.properties")

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _annotation_value(text: str, pattern: str) -> str | None:
        m = re.search(pattern, text, re.MULTILINE)
        return m.group(1) if m else None

    @staticmethod
    def _class_name(text: str) -> str | None:
        m = re.search(r"(?:public\s+)?class\s+([A-Za-z0-9_]+)", text)
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
        roles += re.findall(r'hasRole\(["\']([^"\']+)["\']', text)
        roles += re.findall(r'hasAuthority\(["\']([^"\']+)["\']', text)
        return sorted(set(roles))

    @staticmethod
    def _extract_javadoc_before(text: str, snippet: str) -> str:
        idx = text.find(snippet)
        if idx < 0:
            return ""
        block = text[max(0, idx - 400):idx]
        m = re.search(r"/\*\*([\s\S]*?)\*/\s*$", block)
        if not m:
            return ""
        return re.sub(r"\s*\*\s*", " ", m.group(1)).strip()[:200]

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
