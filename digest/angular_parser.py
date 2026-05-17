from __future__ import annotations

import json
import re
from pathlib import Path

from .models import (
    AngularComponentDigest,
    AngularDigest,
    AngularServiceDigest,
    NgRxFeature,
)


class AngularParser:
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)

    def parse(self) -> AngularDigest:
        modules: list[str] = []
        components: list[AngularComponentDigest] = []
        services: list[AngularServiceDigest] = []
        routes: list[dict] = []
        guards: list[str] = []
        interceptors: list[str] = []
        models: list[str] = []
        ngrx_features: list[NgRxFeature] = []

        for f in self.project_path.rglob("*.module.ts"):
            modules.extend(self._parse_module_file(f))
            if "routing" in f.name:
                routes.extend(self._parse_routes(f))

        for f in self.project_path.rglob("*.component.ts"):
            comp = self._parse_component_file(f)
            if comp:
                components.append(comp)

        for f in self.project_path.rglob("*.service.ts"):
            svc = self._parse_service_file(f)
            if svc:
                services.append(svc)

        for f in self.project_path.rglob("*.guard.ts"):
            content = self._read(f)
            guards.extend(re.findall(r"export\s+(?:class|function|const)\s+([A-Za-z0-9_]+)", content))

        for f in self.project_path.rglob("*.interceptor.ts"):
            content = self._read(f)
            names = re.findall(r"export\s+class\s+([A-Za-z0-9_]+)", content)
            interceptors.extend(names)
            if "Authorization" in content or "Bearer" in content:
                interceptors.append("JWTHeaderInjectionDetected")
            if "refresh" in content.lower() and "token" in content.lower():
                interceptors.append("TokenRefreshDetected")

        for pattern in ("*.model.ts", "*.interface.ts", "*.type.ts", "*.dto.ts"):
            for f in self.project_path.rglob(pattern):
                content = self._read(f)
                models.extend(re.findall(r"(?:export\s+)?(?:interface|enum|class|type)\s+([A-Za-z0-9_]+)", content))

        for f in self.project_path.rglob("*.ts"):
            if "environment" in f.name:
                models.extend(self._extract_env_keys(self._read(f)))

        ngrx_features = self._parse_ngrx()
        environments = self._parse_environments()

        return AngularDigest(
            project=self.project_path.name,
            created_at=self._now(),
            modules=sorted(set(modules)),
            components=components,
            services=services,
            routes=routes,
            guards=sorted(set(guards)),
            interceptors=sorted(set(interceptors)),
            models=sorted(set(models)),
            ngrx_features=ngrx_features,
            environments=environments,
        )

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_module_file(self, file: Path) -> list[str]:
        content = self._read(file)
        return re.findall(r"export\s+class\s+([A-Za-z0-9_]+Module)", content)

    def _parse_component_file(self, file: Path) -> AngularComponentDigest | None:
        content = self._read(file)
        cls = re.search(r"export\s+class\s+([A-Za-z0-9_]+)", content)
        if not cls:
            return None
        selector = ""
        m_sel = re.search(r"selector\s*:\s*['\"]([^'\"]+)['\"]", content)
        if m_sel:
            selector = m_sel.group(1)

        # @Input() varName  |  @Input('alias') varName  |  @Input({ ... }) varName
        inputs = re.findall(r"@Input\s*\([^)]*\)\s+(?:\w+\s+)?(\w+)", content)

        # @Output() varName  |  @Output('alias') varName
        outputs = re.findall(r"@Output\s*\([^)]*\)\s+(?:\w+\s+)?(\w+)", content)

        # Constructor injection + inject() function (Angular 14+)
        injected = self._constructor_types(content)
        injected.extend(self._inject_function_types(content))

        # @ViewChild / @ContentChild references
        view_children = re.findall(
            r"@(?:ViewChild|ContentChild)\s*\(\s*([A-Za-z0-9_]+)", content
        )

        template_events = self._extract_template_events(file)
        methods, method_calls = self._extract_component_methods(content)

        return AngularComponentDigest(
            name=cls.group(1),
            selector=selector,
            file_path=str(file.relative_to(self.project_path)),
            inputs=inputs,
            outputs=outputs,
            injected_services=list(dict.fromkeys(injected)),  # deduplicate, preserve order
            template_events=template_events,
            methods=methods,
            method_calls=method_calls,
            view_children=view_children,
        )

    def _parse_service_file(self, file: Path) -> AngularServiceDigest | None:
        content = self._read(file)
        cls = re.search(r"export\s+class\s+([A-Za-z0-9_]+)", content)
        if not cls:
            return None
        calls = []
        # Extract HTTP calls — try to resolve URL from string literals and template strings
        for m in re.finditer(
            r"this\.http\.(get|post|put|delete|patch)\s*(?:<[^>]*>)?\s*\(([^)]{0,300})\)",
            content,
            re.DOTALL,
        ):
            method = m.group(1).upper()
            args = m.group(2).strip()
            url = self._resolve_url(args.split(",")[0].strip(), content)
            response_type_m = re.search(r"<([A-Za-z0-9_\[\]<>]+)>", m.group(0))
            response_type = response_type_m.group(1) if response_type_m else "unknown"
            calls.append({"method": method, "url": url, "response_shape": response_type})
        return AngularServiceDigest(
            name=cls.group(1),
            file_path=str(file.relative_to(self.project_path)),
            http_calls=calls,
            injected_dependencies=self._constructor_types(content),
        )

    def _parse_routes(self, file: Path) -> list[dict]:
        content = self._read(file)
        rows = []
        for path, component in re.findall(
            r"path\s*:\s*['\"]([^'\"]*)['\"][\s\S]*?component\s*:\s*([A-Za-z0-9_]+)", content
        ):
            rows.append({"path": path, "component": component})
        for path, lazy in re.findall(
            r"path\s*:\s*['\"]([^'\"]*)['\"][\s\S]*?loadChildren\s*:\s*\(\)\s*=>\s*import\(['\"]([^'\"]+)['\"]",
            content,
        ):
            rows.append({"path": path, "component": f"lazy:{lazy}"})
        # canActivate guards
        for path, guard in re.findall(
            r"path\s*:\s*['\"]([^'\"]*)['\"][\s\S]*?canActivate\s*:\s*\[([^\]]+)\]", content
        ):
            rows.append({"path": path, "component": "guarded", "guard": guard.strip()})
        return rows

    def _parse_ngrx(self) -> list[NgRxFeature]:
        """Detect NgRx store features from actions/reducers/effects/selectors files."""
        features: dict[str, NgRxFeature] = {}

        # actions
        for f in self.project_path.rglob("*.actions.ts"):
            feature_name = f.stem.replace(".actions", "")
            content = self._read(f)
            actions = re.findall(r"createAction\s*\(\s*['\"]([^'\"]+)['\"]", content)
            if feature_name not in features:
                features[feature_name] = NgRxFeature(name=feature_name)
            features[feature_name].actions.extend(actions)

        # effects
        for f in self.project_path.rglob("*.effects.ts"):
            feature_name = f.stem.replace(".effects", "")
            content = self._read(f)
            effect_names = re.findall(r"(\w+)\$\s*=\s*createEffect", content)
            if feature_name not in features:
                features[feature_name] = NgRxFeature(name=feature_name)
            features[feature_name].effects.extend(effect_names)

        # selectors
        for f in self.project_path.rglob("*.selectors.ts"):
            feature_name = f.stem.replace(".selectors", "")
            content = self._read(f)
            sel_names = re.findall(r"export\s+const\s+(\w+)\s*=\s*create(?:Selector|Feature)", content)
            if feature_name not in features:
                features[feature_name] = NgRxFeature(name=feature_name)
            features[feature_name].selectors.extend(sel_names)

        # Also detect store usage in generic .ts files (select, dispatch)
        for f in self.project_path.rglob("*.ts"):
            if any(k in f.name for k in (".actions.", ".effects.", ".reducers.", ".selectors.")):
                continue
            content = self._read(f)
            if "Store" in content and ("createAction" in content or "createReducer" in content):
                feature_name = f.stem
                actions = re.findall(r"createAction\s*\(\s*['\"]([^'\"]+)['\"]", content)
                if actions:
                    if feature_name not in features:
                        features[feature_name] = NgRxFeature(name=feature_name)
                    features[feature_name].actions.extend(actions)

        return list(features.values())

    def _parse_environments(self) -> dict:
        """Parse environment.ts / environment.prod.ts files into a dict."""
        envs: dict = {}
        for f in self.project_path.rglob("environment*.ts"):
            content = self._read(f)
            env_name = f.stem  # environment, environment.prod, etc.
            env_data: dict = {}
            # Extract key: value pairs from the exported object
            for key, value in re.findall(r"(\w+)\s*:\s*['\"]([^'\"]+)['\"]", content):
                env_data[key] = value
            for key, value in re.findall(r"(\w+)\s*:\s*(true|false|\d+)", content):
                env_data[key] = value
            if env_data:
                envs[env_name] = env_data
        return envs

    def _extract_template_events(self, component_file: Path) -> list[str]:
        """Extract (click), (submit), (change) bindings from matching template file."""
        template = component_file.with_suffix(".html")
        if not template.exists():
            # Inline template in the component file
            content = self._read(component_file)
            inline = re.search(r"template\s*:\s*`([\s\S]*?)`", content)
            if not inline:
                return []
            html = inline.group(1)
        else:
            html = self._read(template)
        events = re.findall(r"\((\w+)\)\s*=", html)
        return sorted(set(events))

    @staticmethod
    def _resolve_url(raw_url: str, file_content: str) -> str:
        """Try to resolve a URL expression to its string value."""
        raw = raw_url.strip()
        # Already a string literal
        if raw.startswith(("'", '"', "`")):
            return raw.strip("'\"` ")

        # Variable reference — look for const/let assignment in the same file
        m = re.search(rf"(?:const|let|private)\s+{re.escape(raw)}\s*=\s*['\"]([^'\"]+)['\"]", file_content)
        if m:
            return m.group(1)

        # Template literal with a variable: `${this.baseUrl}/path`
        if "${" in raw:
            return re.sub(r"\$\{[^}]+\}", "*", raw).strip("`")

        # `this.someField` — look for field assignment
        field_name = re.sub(r"^this\.", "", raw)
        m = re.search(rf"{re.escape(field_name)}\s*=\s*['\"]([^'\"]+)['\"]", file_content)
        if m:
            return m.group(1)

        return raw  # give up — return as-is

    @staticmethod
    def _constructor_types(content: str) -> list[str]:
        m = re.search(r"constructor\s*\(([\s\S]*?)\)\s*\{", content)
        if not m:
            return []
        types = []
        for part in m.group(1).split(","):
            if ":" in part:
                raw_type = part.split(":")[-1].strip().strip("?").strip()
                raw_type = re.sub(r"<[^>]+>", "", raw_type).strip()
                if raw_type and re.match(r"^[A-Z]", raw_type):
                    types.append(raw_type)
        return types

    @staticmethod
    def _inject_function_types(content: str) -> list[str]:
        """Capture Angular 14+ inject() pattern: private svc = inject(ServiceType)."""
        return re.findall(r"=\s*inject\s*\(\s*([A-Za-z0-9_]+)\s*\)", content)

    @staticmethod
    def _extract_component_methods(content: str) -> tuple[list[str], dict[str, list[str]]]:
        """
        Extract public/non-lifecycle methods and the this.xxx.yyy() calls inside each.
        Skips Angular lifecycle hooks and private angular internals.
        """
        _LIFECYCLE = {
            "ngOnInit", "ngOnDestroy", "ngOnChanges", "ngAfterViewInit",
            "ngAfterContentInit", "ngAfterViewChecked", "ngAfterContentChecked",
            "ngDoCheck", "constructor",
        }
        # Match method declarations: optional modifier + name + (...) + {
        method_re = re.compile(
            r"(?:(?:public|private|protected|async|override)\s+)*"
            r"([a-zA-Z_$][a-zA-Z0-9_$]*)\s*\([^)]{0,200}\)\s*(?::\s*\S+\s*)?\{",
        )
        # Calls inside a method body: this.something.methodName(
        call_re = re.compile(r"this\.(\w+)\.(\w+)\s*\(")

        methods: list[str] = []
        method_calls: dict[str, list[str]] = {}

        for m in method_re.finditer(content):
            name = m.group(1)
            if name in _LIFECYCLE or name[0].isupper():
                continue
            methods.append(name)
            # Grab the body: scan forward from '{' counting braces
            start = m.end() - 1
            depth, i, body_chars = 0, start, []
            while i < len(content) and (depth > 0 or i == start):
                ch = content[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                else:
                    body_chars.append(ch)
                i += 1
            body = "".join(body_chars)
            calls = [f"{obj}.{fn}()" for obj, fn in call_re.findall(body)]
            if calls:
                method_calls[name] = calls

        return methods, method_calls

    @staticmethod
    def _extract_env_keys(content: str) -> list[str]:
        """Return named API URL keys from environment files."""
        keys = []
        for key in ("apiUrl", "baseUrl", "apiBaseUrl", "backendUrl", "serverUrl"):
            m = re.search(rf"{key}\s*:\s*['\"]([^'\"]+)['\"]", content)
            if m:
                keys.append(f"{key}:{m.group(1)}")
        return keys

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
