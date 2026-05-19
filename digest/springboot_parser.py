from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from .models import (
    BeanDigest,
    DtoDigest,
    DtoFieldDigest,
    EndpointDigest,
    EntityDigest,
    EventDigest,
    ExceptionHandlerDigest,
    FeignCallDetail,
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

# NoSQL document/entity annotations — MongoDB, Redis, Neo4j, etc.
_NOSQL_DOCUMENT_ANNOTATIONS = {"Document", "RedisHash", "Node", "DynamoDBTable"}

# Spring Data repository supertypes (covers JPA, MongoDB, Redis, Cassandra, R2DBC, etc.)
# MongoRepository/ReactiveMongoRepository typically have NO @Repository annotation on the interface.
_REPO_SUPERTYPES = {
    "MongoRepository", "ReactiveMongoRepository", "MongoRepositoryBase",
    "JpaRepository", "CrudRepository", "PagingAndSortingRepository",
    "ReactiveCrudRepository", "R2dbcRepository",
    "CassandraRepository", "CouchbaseRepository", "Neo4jRepository",
    "ElasticsearchRepository", "RedisRepository",
}


class SpringBootParser:
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)
        self._pom = PomParser()
        self._constants: dict[str, str] = {}
        self._properties: dict[str, str] = {}

    def parse(self) -> ServiceDigest:
        self._constants = self._build_constant_map()
        self._properties = self._build_properties_map()
        project = self.project_path.name
        java_files = list(self.project_path.rglob("*.java"))

        endpoints: list[EndpointDigest] = []
        entities: list[EntityDigest] = []
        feign_clients: list[FeignClientDigest] = []
        dtos: list[str] = []
        dto_schemas: list[DtoDigest] = []
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
                    dep_names = ast_info.get("dependencies", [])
                    method_names = ast_info.get("method_names", [])
                    method_calls = self._extract_method_call_graph(text, dep_names)
                    queries = self._parse_repository_queries(text) if bean_type == "repository" else []
                    # Extract bodies for service methods (not repositories — they have no bodies)
                    method_bodies = (
                        self._extract_method_bodies(text, method_names)
                        if bean_type == "service"
                        else {}
                    )
                    beans.append(
                        BeanDigest(
                            name=class_name,
                            bean_type=bean_type,
                            file_path=rel,
                            dependencies=dep_names,
                            methods=method_names,
                            transactional_methods=ast_info.get("transactional_methods", []),
                            method_calls=method_calls,
                            queries=queries,
                            method_bodies=method_bodies,
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
            dto_schemas.extend(self._parse_dtos_detailed(text, rel))
            c, p = self._parse_events(text)
            consumes.update(c)
            produces.update(p)
            if "SecurityFilterChain" in text or "WebSecurityConfigurerAdapter" in text:
                security_config.update(self._parse_security(text))

        # Safety net: if AST succeeded but didn't flag has_entity, scan all files.
        # Catches fully-qualified @jakarta.persistence.Entity and all NoSQL @Document variants.
        if not entities:
            for file in java_files:
                text = self._safe_read(file)
                if text and (
                    "@Entity" in text or "persistence.Entity" in text
                    or "@Document" in text or "@RedisHash" in text or "@Node" in text
                ):
                    entities.extend(self._parse_entities(text))

        kafka_topics = self._build_kafka_topic_configs(consumes, produces, "\n".join(
            self._safe_read(f) for f in java_files if self._safe_read(f)
        ))

        security_config.update(self._parse_application_config())

        return ServiceDigest(
            project=project,
            type="spring-boot",
            created_at=self._now(),
            endpoints=endpoints,
            entities=entities,
            dtos=sorted(set(dtos)),
            dto_schemas=dto_schemas,
            feign_clients=feign_clients,
            events=EventDigest(produces=sorted(produces), consumes=sorted(consumes)),
            kafka_topics=kafka_topics,
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

                if "Entity" in ann_names or ann_names & _NOSQL_DOCUMENT_ANNOTATIONS:
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
                # Check annotation-based and supertype-based detection.
                # MongoRepository / ReactiveMongoRepository etc. carry no @Repository annotation.
                extends_names: set[str] = set()
                if iface_node.extends:
                    for ext in (iface_node.extends or []):
                        if hasattr(ext, "name"):
                            extends_names.add(ext.name)
                        elif hasattr(ext, "sub_type") and hasattr(ext.sub_type, "name"):
                            extends_names.add(ext.sub_type.name)
                if ann_names & {"Repository", "RepositoryRestResource"} or extends_names & _REPO_SUPERTYPES:
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
    # Change 4: Method call graph extraction
    # ------------------------------------------------------------------

    def _extract_method_call_graph(self, text: str, dep_names: list[str]) -> dict[str, list[str]]:
        """
        Extract which injected dependency methods each service method calls.
        Returns {method_name: ["dep.call()", ...]}
        Only tracks calls on known injected fields to avoid noise.
        """
        # Build set of field names to track — constructor params + @Autowired fields
        field_names: set[str] = set(dep_names)
        field_names.update(re.findall(
            r'private\s+(?:final\s+)?[\w<>,\s]+\s+(\w+)\s*;', text
        ))
        field_names.update(re.findall(
            r'@Autowired\s+private\s+[\w<>]+\s+(\w+)', text
        ))
        ctor = re.search(r'public\s+\w+\(([^)]+)\)', text)
        if ctor:
            for part in ctor.group(1).split(","):
                parts = part.strip().split()
                if len(parts) >= 2:
                    field_names.add(parts[-1].strip())

        # Remove obvious non-service names
        noise = {"id", "name", "type", "status", "value", "message", "data", "result",
                 "response", "request", "error", "code", "list", "map", "set", "size"}
        field_names = {f for f in field_names if f.lower() not in noise and len(f) > 2}

        # Also always track common Spring infrastructure fields
        infra = {"kafkaTemplate", "rabbitTemplate", "restTemplate", "webClient",
                 "applicationEventPublisher", "objectMapper", "jdbcTemplate"}
        track = field_names | infra

        # Find method boundaries
        method_re = re.compile(
            r'(?:public|private|protected)\s+[\w<>\[\]?,\s]+\s+(\w+)\s*\([^)]*\)'
            r'(?:\s+throws\s+[\w,\s]+)?\s*\{',
            re.MULTILINE,
        )
        matches = list(method_re.finditer(text))

        result: dict[str, list[str]] = {}
        skip = {"if", "while", "for", "switch", "try", "catch", "synchronized"}

        for i, m in enumerate(matches):
            method_name = m.group(1)
            if method_name in skip:
                continue
            body_start = m.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[body_start:body_end]

            calls: list[str] = []
            for field in track:
                for called in re.findall(rf'\b{re.escape(field)}\.([a-zA-Z]\w+)\s*\(', body):
                    if len(called) > 2:
                        calls.append(f"{field}.{called}()")

            if calls:
                result[method_name] = sorted(set(calls))[:8]

        return result

    # ------------------------------------------------------------------
    # Change 5: JPQL / @Query extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_repository_queries(text: str) -> list[str]:
        """Extract @Query JPQL/HQL/SQL strings and describe derived Spring Data query methods."""
        queries: list[str] = []

        # Explicit @Query annotations
        for m in re.finditer(
            r'@Query\s*\(\s*(?:value\s*=\s*)?["\']([^"\']{10,})["\']',
            text, re.MULTILINE,
        ):
            q = m.group(1).strip()
            if q:
                queries.append(q[:300])

        # Derived Spring Data query methods (generated at runtime from method name)
        _DERIVED_PREFIXES = (
            "findBy", "findAllBy", "findFirstBy", "findTopBy",
            "countBy", "existsBy", "deleteBy", "removeBy",
            "streamBy", "readBy", "getBy",
        )
        seen: set[str] = set()
        for m in re.finditer(r'\b([a-z][A-Za-z0-9]+By[A-Za-z0-9]+)\s*\(', text):
            method_sig = m.group(1)
            if method_sig in seen:
                continue
            if any(method_sig.startswith(p) for p in _DERIVED_PREFIXES):
                seen.add(method_sig)
                desc = SpringBootParser._describe_derived_query(method_sig)
                queries.append(f"[derived] {method_sig}: {desc}")

        return queries

    @staticmethod
    def _describe_derived_query(method_name: str) -> str:
        """
        Translate a Spring Data derived query method name into a human-readable description.
        e.g. findByEsaAndProducttype → SELECT WHERE esa = ? AND producttype = ?
        """
        _PREFIX_MAP = {
            "findBy": "SELECT WHERE", "findAllBy": "SELECT ALL WHERE",
            "findFirstBy": "SELECT FIRST WHERE", "findTopBy": "SELECT TOP WHERE",
            "countBy": "COUNT WHERE", "existsBy": "EXISTS WHERE",
            "deleteBy": "DELETE WHERE", "removeBy": "DELETE WHERE",
            "streamBy": "STREAM WHERE", "readBy": "SELECT WHERE", "getBy": "SELECT WHERE",
        }
        # Strip known prefix
        action = "SELECT WHERE"
        remainder = method_name
        for prefix, desc in _PREFIX_MAP.items():
            if method_name.startswith(prefix):
                action = desc
                remainder = method_name[len(prefix):]
                break

        # Strip trailing OrderBy / Limit / Top / First clauses
        order_m = re.search(r'OrderBy[A-Z].*$', remainder)
        order_clause = ""
        if order_m:
            order_text = re.sub(r'([A-Z])', r' \1', order_m.group(0)[7:]).lower().strip()
            order_clause = f" ORDER BY {order_text}"
            remainder = remainder[:order_m.start()]

        # Split on And / Or (keep delimiter)
        parts = re.split(r'(And|Or)', remainder)
        conditions: list[str] = []
        i = 0
        while i < len(parts):
            field = parts[i]
            if not field or field in ("And", "Or"):
                i += 1
                continue
            connector = ""
            if i + 1 < len(parts) and parts[i + 1] in ("And", "Or"):
                connector = " " + parts[i + 1].upper()
                i += 1

            # Detect keyword suffixes: IsNull, IsNotNull, Like, In, Between, GreaterThan, etc.
            _SUFFIX_MAP = {
                "IsNull": "IS NULL", "IsNotNull": "IS NOT NULL",
                "Like": "LIKE ?", "NotLike": "NOT LIKE ?",
                "In": "IN (?)", "NotIn": "NOT IN (?)",
                "Between": "BETWEEN ? AND ?",
                "GreaterThan": "> ?", "GreaterThanEqual": ">= ?",
                "LessThan": "< ?", "LessThanEqual": "<= ?",
                "Before": "< ?", "After": "> ?",
                "True": "= true", "False": "= false",
                "Containing": "LIKE %?%", "StartingWith": "LIKE ?%", "EndingWith": "LIKE %?",
            }
            col_name = re.sub(r'([A-Z])', r'_\1', field).lstrip('_').lower()
            condition = f"{col_name} = ?"
            for suffix, sql in _SUFFIX_MAP.items():
                if field.endswith(suffix):
                    col_name2 = re.sub(r'([A-Z])', r'_\1', field[:-len(suffix)]).lstrip('_').lower()
                    condition = f"{col_name2} {sql}"
                    break

            conditions.append(condition + connector)
            i += 1

        cond_str = " ".join(conditions) if conditions else remainder
        return f"{action} {cond_str}{order_clause}"

    @staticmethod
    def _extract_method_bodies(text: str, method_names: list[str]) -> dict[str, str]:
        """
        Extract the body of each named method from Java source using brace-depth scanning.
        Returns {method_name: body_text} capped at 500 chars.
        Skips trivial methods: bare getters (return field;) and setters (this.x = x;).
        """
        _TRIVIAL = re.compile(
            r'^\s*\{?\s*(?:return\s+\w+\s*;|this\.\w+\s*=\s*\w+\s*;)\s*\}?\s*$'
        )
        bodies: dict[str, str] = {}
        for name in method_names:
            # Find method signature: optional modifiers + name + (
            sig_pattern = re.compile(
                rf'(?:(?:public|private|protected|static|final|synchronized|async|override)\s+)*'
                rf'[\w<>\[\],\s]+\s+{re.escape(name)}\s*\(',
            )
            m = sig_pattern.search(text)
            if not m:
                continue
            # Scan forward to the opening { of the method body
            brace_start = text.find('{', m.start())
            if brace_start == -1:
                continue
            # Collect body via brace-depth counting
            depth = 0
            end = brace_start
            for i in range(brace_start, min(brace_start + 3000, len(text))):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            body = text[brace_start:end].strip()
            if not body or _TRIVIAL.match(body):
                continue
            bodies[name] = body[:500]
        return bodies

    # ------------------------------------------------------------------
    # Regex-based extraction (kept from v1, used for annotation values)
    # ------------------------------------------------------------------

    def _parse_controllers(self, text: str) -> list[EndpointDigest]:
        if "@RestController" not in text and "@Controller" not in text:
            return []
        # class_base from the class-level @RequestMapping (full text needed)
        raw_base = self._annotation_value(text, r"@RequestMapping\(([\s\S]*?)\)") or ""
        class_base = self._extract_path_from_mapping_args(raw_base) if raw_base else ""
        controller_name = self._class_name(text) or "UnknownController"
        rows: list[EndpointDigest] = []

        # Scan only the class BODY so the class-level @RequestMapping is never in scope.
        # This prevents it from consuming method-level annotations via [\s\S]*? matching.
        cls_decl = re.search(r'\bclass\s+\w+', text)
        body_start = text.find("{", cls_decl.start() if cls_decl else 0) + 1
        class_body = text[body_start:]

        method_pattern = re.compile(
            # [\s\S]*? inside annotation parens captures multi-line annotation args
            r"(@(?:GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)\(([\s\S]*?)\))"
            r"(?:\s*@(?!(?:Get|Post|Put|Delete|Patch)Mapping|Rest|Controller|RequestMapping\b)[^\n]*)*\s*"
            r"(public\s+([A-Za-z0-9_<>, ?]+)\s+([A-Za-z0-9_]+)\s*\((.*?)\))",
            re.MULTILINE,
        )
        for m in method_pattern.finditer(class_body):
            ann_block, ann_args, _, response_type, handler, params = m.groups()
            # Include 200 chars before the match in class_body to catch @PreAuthorize above the mapping
            context = class_body[max(0, m.start() - 200): m.end()]
            ann_name = re.search(r"@([A-Za-z]+)\(", ann_block)
            ann = ann_name.group(1) if ann_name else "RequestMapping"
            http_method = HTTP_ANN_TO_METHOD.get(ann, "GET")
            if ann == "RequestMapping":
                req_method = re.search(r"RequestMethod\.([A-Z]+)", context)
                if req_method:
                    http_method = req_method.group(1)
            method_path = self._extract_path_from_mapping_args(ann_args)
            full_path = self._join_paths(class_base, method_path)
            request_dto = self._extract_request_body_type(params)
            roles = self._extract_roles(context)
            auth_required = bool(roles) or "@PreAuthorize" in context or "@Secured" in context
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
        has_jpa = "@Entity" in text or "persistence.Entity" in text
        has_nosql = "@Document" in text or "@RedisHash" in text or "@Node" in text
        if not has_jpa and not has_nosql:
            return []

        name = self._class_name(text) or "UnknownEntity"

        # ── Table / collection name ───────────────────────────────────────
        if has_jpa:
            raw_table = self._annotation_value(text, r"@Table\(([\s\S]*?)\)") or name
            table_name = self._extract_name_arg(raw_table)
        else:
            table_name = self._extract_document_collection(text, name)

        # ── Fields ───────────────────────────────────────────────────────
        # JPA uses @Column; MongoDB uses @Field; both fall back to bare private fields.
        col_fields = re.findall(
            r"@(?:Column|Field)(?:\([^)]*\))?\s+(?:@\w+\s+)*private\s+[\w<>?,\[\] ]+\s+(\w+)\s*[;=]",
            text,
        )
        # @Id (JPA) and org.springframework.data.annotation.@Id (Spring Data / MongoDB)
        id_fields = re.findall(
            r"@Id\b[\s\S]{0,120}?private\s+[\w<>?,\[\] ]+\s+(\w+)\s*[;=]", text
        )
        all_fields = list(dict.fromkeys(id_fields + col_fields))

        # Fallback: Lombok-style bare private fields when no @Column/@Field found
        if not col_fields:
            body_start = text.find("{", text.find(f"class {name}") if name != "UnknownEntity" else 0)
            if body_start > 0:
                body = text[body_start: body_start + 4000]
                _SKIP = {"log", "logger", "LOGGER", "serialVersionUID", "INSTANCE"}
                extra = re.findall(
                    r"private\s+(?:final\s+)?(?!static\b)[\w<>?,\[\] ]+\s+(\w+)\s*[;=]", body
                )
                all_fields = list(dict.fromkeys(f for f in extra if f not in _SKIP))

        # ── Relationships ─────────────────────────────────────────────────
        relationships: list[str] = []
        # JPA relationships
        for ann, label in {
            "OneToMany": "OneToMany", "ManyToOne": "ManyToOne",
            "ManyToMany": "ManyToMany", "OneToOne": "OneToOne",
        }.items():
            for target in re.findall(
                rf"@{ann}[\s\S]*?private\s+[A-Za-z0-9_<>, ?]+\s+([A-Za-z0-9_]+)\s*;", text
            ):
                relationships.append(f"{label} -> {target}")
        # MongoDB @DBRef (document reference to another collection)
        for target in re.findall(
            r"@DBRef[\s\S]{0,100}?private\s+[A-Za-z0-9_<>, ?]+\s+([A-Za-z0-9_]+)\s*;", text
        ):
            relationships.append(f"DBRef -> {target}")

        return [EntityDigest(
            name=name,
            table=table_name,
            fields=all_fields,
            relationships=relationships,
        )]

    def _extract_document_collection(self, text: str, class_name: str) -> str:
        """
        Extract the MongoDB/NoSQL collection name from @Document annotation.

        Handles:
          @Document("collectionName")
          @Document(collection = "collectionName")
          @Document("base#{@environment.getProperty('key') ?: ''}")
              → resolves 'key' from application.properties, e.g. "base_dev"
          Java string concatenation split across lines:
              @Document("base#{@environment.getProperty"
                      + "('key') ?: ''}")
          @Document with no args → class name
        """
        # Locate the opening paren of @Document(...) and collect the full argument
        # by counting paren depth — handles nested parens inside SpEL/strings.
        doc_m = re.search(r'@Document\s*\(', text)
        if not doc_m:
            return class_name
        start = doc_m.end() - 1   # position of the opening '('
        depth = 0
        end = start
        for i in range(start, min(start + 800, len(text))):
            if text[i] == '(':
                depth += 1
            elif text[i] == ')':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        raw_args = text[start + 1: end].strip()
        if not raw_args:
            return class_name

        # Join Java string concatenation: "part1"  \n  + "part2" → "part1part2"
        joined = re.sub(r'"\s*\n\s*\+\s*"', '', raw_args)

        # collection = "name" named-attribute form (single quotes allowed inside)
        coll_m = re.search(r'collection\s*=\s*"([^"]+)"', joined)
        raw_value = coll_m.group(1) if coll_m else None

        if raw_value is None:
            # Positional string argument — capture the full double-quoted Java string
            # (single quotes inside are allowed: SpEL uses them for property keys)
            str_m = re.search(r'"([^"]+)"', joined)
            raw_value = str_m.group(1) if str_m else None

        if not raw_value:
            return class_name

        # Resolve SpEL: #{@environment.getProperty('key') ?: 'default'}
        spel_idx = raw_value.find('#{')
        if spel_idx >= 0:
            base_name = raw_value[:spel_idx]
            spel_expr = raw_value[spel_idx:]
            # Extract the property key from getProperty('key')
            prop_m = re.search(r"getProperty\s*\(\s*['\"]([^'\"]+)['\"]", spel_expr)
            if prop_m:
                prop_key = prop_m.group(1)
                suffix = self._properties.get(prop_key, "")
                return base_name + suffix   # e.g. "esazonemapping" + "" or "_dev"
            return base_name

        return raw_value.strip()

    def _parse_dtos(self, text: str) -> list[str]:
        classes = re.findall(r"class\s+([A-Za-z0-9_]+)", text)
        return [c for c in classes if any(token in c for token in ("DTO", "Dto", "Request", "Response", "Payload"))]

    def _parse_dtos_detailed(self, text: str, rel_path: str) -> list[DtoDigest]:
        """
        Extract DTO field structures from Request/Response/DTO/Payload classes.
        Uses javalang AST when available, falls back to regex.
        """
        _DTO_TOKENS = ("DTO", "Dto", "Request", "Response", "Payload", "Model", "Body")
        if not any(t in text for t in _DTO_TOKENS):
            return []

        try:
            import javalang
            tree = javalang.parse.parse(text)
            results: list[DtoDigest] = []
            for _, cls_node in tree.filter(javalang.tree.ClassDeclaration):
                if not any(t in cls_node.name for t in _DTO_TOKENS):
                    continue
                fields = self._extract_dto_fields_ast(cls_node)
                results.append(DtoDigest(name=cls_node.name, file_path=rel_path, fields=fields))
            return results
        except Exception:
            return self._parse_dtos_detailed_regex(text, rel_path)

    def _extract_dto_fields_ast(self, cls_node) -> list[DtoFieldDigest]:
        _VALIDATION_ANNS = {
            "NotNull", "NotEmpty", "NotBlank", "Size", "Min", "Max",
            "Pattern", "Email", "Valid", "Positive", "Negative", "DecimalMin", "DecimalMax",
        }
        fields: list[DtoFieldDigest] = []
        for field_decl in (cls_node.fields or []):
            try:
                field_anns = {a.name: a for a in (field_decl.annotations or [])}
                # type name
                ft = field_decl.type
                type_str = ft.name if hasattr(ft, "name") else str(ft)
                if hasattr(ft, "arguments") and ft.arguments:
                    args = ", ".join(
                        a.type.name if hasattr(a, "type") and hasattr(a.type, "name") else str(a)
                        for a in ft.arguments
                    )
                    type_str = f"{type_str}<{args}>"

                validations = [a for a in field_anns if a in _VALIDATION_ANNS]
                required = bool({"NotNull", "NotEmpty", "NotBlank"} & set(validations))

                json_prop = ""
                if "JsonProperty" in field_anns:
                    ann = field_anns["JsonProperty"]
                    try:
                        elem = ann.element
                        if isinstance(elem, list):
                            for pair in elem:
                                if hasattr(pair, "value"):
                                    json_prop = str(getattr(pair.value, "value", "")).strip('"')
                        else:
                            json_prop = str(getattr(elem, "value", "")).strip('"')
                    except Exception:
                        pass

                for declarator in (field_decl.declarators or []):
                    fields.append(DtoFieldDigest(
                        name=declarator.name,
                        type=type_str,
                        required=required,
                        json_property=json_prop,
                        validations=validations,
                    ))
            except Exception:
                continue
        return fields

    def _parse_dtos_detailed_regex(self, text: str, rel_path: str) -> list[DtoDigest]:
        """Regex fallback for DTO field extraction."""
        _DTO_TOKENS = ("DTO", "Dto", "Request", "Response", "Payload", "Model", "Body")
        _SKIP_FIELDS = {"serialVersionUID", "log", "logger", "LOGGER", "INSTANCE"}
        _VALIDATION_ANNS = {
            "NotNull", "NotEmpty", "NotBlank", "Size", "Min", "Max",
            "Pattern", "Email", "Valid", "Positive",
        }

        results: list[DtoDigest] = []
        cls_m = re.search(r"(?:public\s+)?class\s+([A-Za-z0-9_]+)\b", text)
        if not cls_m:
            return []
        cls_name = cls_m.group(1)
        if not any(t in cls_name for t in _DTO_TOKENS):
            return []

        body_start = text.find("{", cls_m.start())
        if body_start < 0:
            return []
        body = text[body_start:]

        fields: list[DtoFieldDigest] = []
        field_re = re.compile(
            r"((?:@\w+(?:\([^)]*\))?\s+)+)?"
            r"(?:private|protected|public)\s+(?:final\s+)?"
            r"([\w<>?,\[\] ]+?)\s+([a-z]\w*)\s*[;=]",
            re.MULTILINE,
        )
        for fm in field_re.finditer(body):
            anns_block = fm.group(1) or ""
            field_type = fm.group(2).strip()
            field_name = fm.group(3)
            if field_name in _SKIP_FIELDS:
                continue

            validations = re.findall(
                r"@(" + "|".join(_VALIDATION_ANNS) + r")\b", anns_block
            )
            json_prop_m = re.search(r'@JsonProperty\s*\(\s*["\']([^"\']+)["\']', anns_block)
            json_prop = json_prop_m.group(1) if json_prop_m else ""
            required = bool({"NotNull", "NotEmpty", "NotBlank"} & set(validations))

            fields.append(DtoFieldDigest(
                name=field_name,
                type=field_type,
                required=required,
                json_property=json_prop,
                validations=validations,
            ))

        if fields:
            results.append(DtoDigest(name=cls_name, file_path=rel_path, fields=fields))
        return results

    def _parse_feign(self, text: str) -> list[FeignClientDigest]:
        if "@FeignClient" not in text:
            return []
        # [\s\S]*? captures multi-line @FeignClient annotations
        m = re.search(r"@FeignClient\(([\s\S]*?)\)", text)
        raw = m.group(1) if m else ""
        client_name = self._extract_named_arg(raw, "name") or self._extract_named_arg(raw, "value") or "unknown"
        target = self._extract_named_arg(raw, "contextId") or client_name

        # Resolve URL from property placeholder: url = "${ms-java.appointments.url}"
        url_raw = self._extract_named_arg(raw, "url") or ""
        url_prop_key = ""
        resolved_url = ""
        if url_raw:
            prop_m = re.match(r"^\$\{([^}]+)\}$", url_raw.strip())
            if prop_m:
                url_prop_key = prop_m.group(1)
                resolved_url = self._properties.get(url_prop_key, "")
            else:
                resolved_url = url_raw

        # Infer target service from resolved URL hostname or property key
        if resolved_url:
            host_m = re.search(r"https?://([^/:]+)", resolved_url)
            if host_m:
                target = host_m.group(1)
        elif url_prop_key:
            parts = url_prop_key.rstrip(".").split(".")
            if len(parts) >= 2 and parts[-1] in ("url", "host", "base", "uri", "endpoint"):
                parts = parts[:-1]
            target = "-".join(parts)

        # Parse each method in the Feign interface with full request/response type info
        calls: list[str] = []
        call_details: list[FeignCallDetail] = []

        feign_method_re = re.compile(
            r"@((?:Get|Post|Put|Delete|Patch)Mapping|RequestMapping)\((.*?)\)\s*"
            r"(?:@(?!(?:Get|Post|Put|Delete|Patch)Mapping|RequestMapping)\w+[^\n]*\n\s*)*"
            r"([\w<>?,\[\] ]+?)\s+(\w+)\s*\(([^)]*)\)\s*;",
            re.MULTILINE,
        )
        for fm in feign_method_re.finditer(text):
            ann, ann_args, ret_type, _method_name, params = fm.groups()
            path = self._extract_path_from_mapping_args(ann_args)
            http_method = HTTP_ANN_TO_METHOD.get(ann, "GET")
            if ann == "RequestMapping":
                explicit = re.search(r"RequestMethod\.([A-Z]+)", ann_args)
                if explicit:
                    http_method = explicit.group(1)

            calls.append(f"{http_method} {path}")

            # Unwrap return type: List<Foo> → Foo, ResponseEntity<Foo> → Foo
            resp_dto = ret_type.strip()
            for wrapper in ("ResponseEntity", "List", "Optional", "Mono", "Flux", "Set"):
                inner_m = re.match(rf"^{wrapper}<(.+)>$", resp_dto)
                if inner_m:
                    resp_dto = inner_m.group(1).strip()
                    break

            # Extract @RequestBody type from params
            req_dto = ""
            rb_m = re.search(r"@RequestBody\s+(?:[\w<>]+\s+)?(\w[\w<>]*)\s+\w", params)
            if rb_m:
                req_dto = rb_m.group(1)

            # Extract @PathVariable names
            path_params = re.findall(r"@PathVariable(?:\([^)]*\))?\s+(?:[\w<>]+\s+)?(\w+)", params)

            call_details.append(FeignCallDetail(
                method=http_method,
                path=path,
                request_dto=req_dto,
                response_dto=resp_dto if resp_dto not in ("void", "Void", "Object") else "",
                path_params=path_params,
            ))

        return [FeignClientDigest(
            client_name=client_name,
            target_service=target,
            calls=calls,
            call_details=call_details,
            resolved_url=resolved_url,
            url_property_key=url_prop_key,
        )]

    def _build_properties_map(self) -> dict[str, str]:
        """Scan application.properties / application.yml files to resolve property placeholders."""
        props: dict[str, str] = {}
        for file in self._iter_config_files():
            text = self._safe_read(file)
            if not text:
                continue
            if file.suffix in (".yml", ".yaml"):
                # Flat key: value lines (non-nested)
                for pm in re.finditer(r"^([\w.\-]+)\s*:\s*([^\n#]+)", text, re.MULTILINE):
                    key = pm.group(1).strip()
                    val = pm.group(2).strip().strip("'\"")
                    if val and not val.startswith("{"):
                        props[key] = val
            else:
                # .properties: key=value or key: value
                for pm in re.finditer(r"^([\w.\-]+)\s*[=:]\s*([^\n#]+)", text, re.MULTILINE):
                    key = pm.group(1).strip()
                    val = pm.group(2).strip()
                    if val:
                        props[key] = val
        return props

    def _parse_events(self, text: str) -> tuple[set[str], set[str]]:
        """
        Extract Kafka/RabbitMQ produce and consume topics from a Java source file.

        Handles all common patterns:
        - Literal string topics: @KafkaListener(topics = "order.events")
        - Property placeholder topics: @KafkaListener(topics = "${kafka.topic.orders}")
        - Array topics: @KafkaListener(topics = {"t1", "t2"})
        - @Value-injected field topics: kafkaTemplate.send(ordersTopic, msg)
        - Constant-referenced topics: kafkaTemplate.send(ORDER_TOPIC, msg)
        - topicPattern: @KafkaListener(topicPattern = "orders\\..*")
        - Spring Cloud Stream: streamBridge.send("channel-name", msg)
        - RabbitMQ: @RabbitListener, rabbitTemplate
        """
        # ── Step 1: build a local map of field-name → resolved topic value ──────
        # Covers both @Value("${...}") and static final String constants
        topic_fields: dict[str, str] = {}

        # @Value("${kafka.topic.orders}") private String ordersTopic;
        for m in re.finditer(
            r'@Value\s*\(\s*"?\$\{([^}]+)\}"?\s*\)\s+(?:private\s+|protected\s+)?(?:final\s+)?'
            r'String\s+(\w+)',
            text,
        ):
            prop_key, field = m.group(1), m.group(2)
            resolved = self._properties.get(prop_key, "")
            if resolved:
                topic_fields[field] = resolved

        # static final String ORDER_TOPIC = "order.events";  (from self._constants or inline)
        for m in re.finditer(
            r'(?:private\s+|public\s+|protected\s+)?static\s+final\s+String\s+(\w+)\s*=\s*"([^"]+)"',
            text,
        ):
            const_name, value = m.group(1), m.group(2)
            topic_fields[const_name] = value

        # Also check the pre-built constant map for ClassName.FIELD references
        for ref, value in self._constants.items():
            short = ref.split(".")[-1]   # "OrderConstants.TOPIC" → "TOPIC"
            topic_fields.setdefault(short, value)
            topic_fields.setdefault(ref, value)

        def _resolve(raw: str) -> str:
            """Resolve a topic reference: literal, ${prop}, SpEL #{'${prop}'}, or field/constant."""
            raw = raw.strip().strip('"\'')
            # SpEL expression: #{...}
            # Handles: #{'${prop}'}, #{'literal'}, #{T(Class).FIELD}
            spel_m = re.match(r"#\{(.+)\}$", raw)
            if spel_m:
                inner = spel_m.group(1).strip()
                # Strip SpEL string literal quotes: #{'...'} → inner content
                if inner.startswith("'") and inner.endswith("'"):
                    inner = inner[1:-1]
                # Inner may now be ${prop} or a literal topic
                if inner.startswith("${") and inner.endswith("}"):
                    return self._properties.get(inner[2:-1], inner)
                # #{T(ClassName).FIELD} — look up the field name
                t_match = re.match(r"T\([^)]+\)\.(\w+)", inner)
                if t_match:
                    return topic_fields.get(t_match.group(1), inner)
                return inner  # plain SpEL literal
            # Simple property placeholder: ${prop.key}
            if raw.startswith("${") and raw.endswith("}"):
                return self._properties.get(raw[2:-1], raw)
            return topic_fields.get(raw, raw)

        # ── Step 2: Kafka consumers ───────────────────────────────────────────────
        consumes: set[str] = set()

        # @KafkaListener(topics = "...") — capture full quoted string so SpEL isn't truncated
        for m in re.finditer(
            r'@KafkaListener\s*\([\s\S]*?topics\s*=\s*"([^"]+)"',
            text,
        ):
            t = _resolve(m.group(1))
            if t and not t.startswith("@") and not t.startswith("#"):
                consumes.add(t)

        # @KafkaListener(topics = ${prop}) — bare placeholder without surrounding quotes
        for m in re.finditer(
            r'@KafkaListener\s*\([\s\S]*?topics\s*=\s*(\$\{[^}]+\})',
            text,
        ):
            t = _resolve(m.group(1))
            if t and not t.startswith("@"):
                consumes.add(t)

        # @KafkaListener(topics = {"t1", "${t2}"}) — array format
        for m in re.finditer(r'@KafkaListener\s*\([\s\S]*?topics\s*=\s*\{([^}]+)\}', text):
            for entry in re.findall(r'"([^"]+)"', m.group(1)):
                consumes.add(_resolve(entry))

        # @KafkaListener(topicPattern = "orders\\..*")
        for m in re.finditer(r'@KafkaListener\s*\([\s\S]*?topicPattern\s*=\s*"([^"]+)"', text):
            consumes.add(f"pattern:{m.group(1)}")

        # RabbitMQ
        for m in re.finditer(
            r'@RabbitListener\s*\([\s\S]*?queues\s*=\s*(?:\{([^}]+)\}|"([^"]+)")',
            text,
        ):
            for entry in re.findall(r'"([^"]+)"', m.group(0)):
                consumes.add(_resolve(entry))

        # ── Step 3: Kafka producers ───────────────────────────────────────────────
        produces: set[str] = set()

        # kafkaTemplate.send("literal", ...) or kafkaTemplate.send(fieldName, ...)
        for m in re.finditer(
            r'(?:kafkaTemplate|kafkaSender)\s*\.\s*\w+\s*\(\s*("?[\w.${}"-]+"?)\s*,',
            text,
        ):
            t = _resolve(m.group(1))
            if t and not t.startswith("@"):
                produces.add(t)

        # rabbitTemplate.convertAndSend("exchange", "routingKey", ...)
        for m in re.finditer(
            r'rabbitTemplate\s*\.\s*\w+\s*\(\s*"([^"]+)"',
            text,
        ):
            produces.add(m.group(1))

        # streamBridge.send("channel-name", msg) — Spring Cloud Stream
        for m in re.finditer(r'streamBridge\s*\.\s*send\s*\(\s*"([^"]+)"', text):
            produces.add(m.group(1))

        # Generic .send("literal", ...) on any template-like object
        for m in re.finditer(r'\.send\s*\(\s*"([A-Za-z0-9._-]+)"', text):
            produces.add(m.group(1))

        # .send(fieldName, ...) — resolve field/constant names
        for m in re.finditer(r'\.send\s*\(\s*([A-Za-z_]\w*)\s*,', text):
            t = topic_fields.get(m.group(1), "")
            if t:
                produces.add(t)

        # Clean up: remove obviously wrong values (Java keywords, empty, too short)
        _bad = {"null", "true", "false", "this", "new", "super", "return"}
        consumes = {t for t in consumes if t and len(t) > 1 and t not in _bad}
        produces = {t for t in produces if t and len(t) > 1 and t not in _bad}

        return consumes, produces

    def _build_kafka_topic_configs(
        self, consumes: set[str], produces: set[str], text_all: str
    ) -> list:
        """
        Build structured KafkaTopicConfig entries by cross-referencing detected
        topics with spring.kafka.* properties and publisher REST endpoints.
        """
        from .models import KafkaTopicConfig
        configs: list[KafkaTopicConfig] = []

        # Build reverse map: topic_value → property_key (e.g. "order.events" → "spring.kafka.consumer.topic")
        value_to_key: dict[str, str] = {}
        for key, val in self._properties.items():
            if "kafka" in key.lower() and ("topic" in key.lower()):
                value_to_key[val] = key

        # Group ID from properties
        group_id = (
            self._properties.get("spring.kafka.consumer.group-id", "")
            or self._properties.get("kafka.consumer.group-id", "")
        )

        # Consumer topics
        for topic in consumes:
            prop_key = value_to_key.get(topic, "")
            if not prop_key:
                # Try to find matching key by value
                for k, v in self._properties.items():
                    if v == topic and "topic" in k.lower():
                        prop_key = k
                        break
            configs.append(KafkaTopicConfig(
                topic_name=topic,
                role="consumer",
                property_key=prop_key,
                group_id=group_id,
            ))

        # Producer topics — detect if produced via a REST publisher endpoint
        # Scan for methods that contain BOTH an HTTP mapping AND kafkaTemplate.send()
        publisher_endpoints: dict[str, str] = {}  # topic → "METHOD /path"
        method_re = re.compile(
            r"(@(?:GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)"
            r"\(([\s\S]*?)\))[\s\S]{0,600}?kafkaTemplate[\s\S]{0,200}?\.send\s*\(",
            re.MULTILINE,
        )
        # Class-level base path
        raw_base = self._annotation_value(text_all, r"@RequestMapping\(([\s\S]*?)\)") or ""
        base = self._extract_path_from_mapping_args(raw_base) if raw_base else ""

        cls_decl = re.search(r'\bclass\s+\w+', text_all)
        body_start = text_all.find("{", cls_decl.start() if cls_decl else 0) + 1
        class_body = text_all[body_start:]

        for m in method_re.finditer(class_body):
            ann_block, ann_args = m.group(1), m.group(2)
            ann_name = re.search(r"@([A-Za-z]+)\(", ann_block)
            ann = ann_name.group(1) if ann_name else "RequestMapping"
            http_method = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
                           "DeleteMapping": "DELETE", "PatchMapping": "PATCH"}.get(ann, "POST")
            path = self._extract_path_from_mapping_args(ann_args)
            full_path = self._join_paths(base, path) if path else base
            ep_str = f"{http_method} {full_path}"
            # Associate all produced topics with this publisher endpoint
            for topic in produces:
                if topic not in publisher_endpoints:
                    publisher_endpoints[topic] = ep_str

        for topic in produces:
            prop_key = value_to_key.get(topic, "")
            if not prop_key:
                for k, v in self._properties.items():
                    if v == topic and "topic" in k.lower():
                        prop_key = k
                        break
            existing = next((c for c in configs if c.topic_name == topic), None)
            if existing:
                existing.role = "both"
            else:
                configs.append(KafkaTopicConfig(
                    topic_name=topic,
                    role="producer",
                    property_key=prop_key,
                    group_id=group_id,
                    publisher_endpoint=publisher_endpoints.get(topic, ""),
                ))

        return configs

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

    def _extract_path_from_mapping_args(self, args: str) -> str:
        """Extract path from mapping annotation args, resolving Java constants when needed."""
        # Named arg with quoted value
        key_match = re.search(r'(?:path|value)\s*=\s*"([^"]+)"', args)
        if key_match:
            return key_match.group(1)
        # Direct quoted string
        quote = re.search(r'"([^"]+)"', args)
        if quote:
            return quote.group(1)
        # Constant reference: strip optional "value = " / "path = " prefix then look up
        stripped = re.sub(r'^(?:path|value)\s*=\s*', '', args.strip())
        const_m = re.search(r'\b([A-Z]\w*(?:\.[A-Z_]\w*)*)\b', stripped)
        if const_m:
            ref = const_m.group(1)
            resolved = self._constants.get(ref)
            if not resolved:
                # Try just the field name after the last dot
                short = ref.rsplit(".", 1)[-1]
                resolved = self._constants.get(short)
            if resolved:
                return resolved
        return ""

    def _build_constant_map(self) -> dict[str, str]:
        """Scan all Java files in the project to build ClassName.FIELD → "value" map."""
        constants: dict[str, str] = {}
        for file in self.project_path.rglob("*.java"):
            text = self._safe_read(file)
            if not text or "static final String" not in text:
                continue
            cls = self._class_name(text) or ""
            for m in re.finditer(
                r'(?:public\s+|protected\s+|private\s+)?'
                r'(?:static\s+final|final\s+static)\s+String\s+(\w+)\s*=\s*"([^"]*)"',
                text,
            ):
                field, value = m.group(1), m.group(2)
                if cls:
                    constants[f"{cls}.{field}"] = value
                constants[field] = value   # short form: SITE_BASE_URL → "/api/sites"
        return constants

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
