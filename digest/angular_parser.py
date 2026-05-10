from __future__ import annotations

import re
from pathlib import Path

from .models import AngularComponentDigest, AngularDigest, AngularServiceDigest


class AngularParser:
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)

    def parse(self) -> AngularDigest:
        modules = []
        components = []
        services = []
        routes = []
        guards = []
        interceptors = []
        models = []

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
            guards.extend(re.findall(r"export\s+class\s+([A-Za-z0-9_]+)", self._read(f)))
        for f in self.project_path.rglob("*.interceptor.ts"):
            content = self._read(f)
            interceptors.extend(re.findall(r"export\s+class\s+([A-Za-z0-9_]+)", content))
            if "Authorization" in content:
                interceptors.append("JWTHeaderInjectionDetected")
        for pattern in ("*.model.ts", "*.interface.ts"):
            for f in self.project_path.rglob(pattern):
                content = self._read(f)
                models.extend(re.findall(r"(?:interface|enum|class)\s+([A-Za-z0-9_]+)", content))
        for f in self.project_path.rglob("*.ts"):
            if "environment" in f.name:
                models.extend(self._extract_env_urls(self._read(f)))

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
        )

    def _parse_module_file(self, file: Path) -> list[str]:
        content = self._read(file)
        return re.findall(r"export\s+class\s+([A-Za-z0-9_]+Module)", content)

    def _parse_component_file(self, file: Path) -> AngularComponentDigest | None:
        content = self._read(file)
        cls = re.search(r"export\s+class\s+([A-Za-z0-9_]+)", content)
        if not cls:
            return None
        selector = ""
        m_sel = re.search(r"selector\s*:\s*'([^']+)'", content)
        if m_sel:
            selector = m_sel.group(1)
        inputs = re.findall(r"@Input\(\)\s+([A-Za-z0-9_]+)", content)
        outputs = re.findall(r"@Output\(\)\s+([A-Za-z0-9_]+)", content)
        injected = self._constructor_types(content)
        return AngularComponentDigest(
            name=cls.group(1),
            selector=selector,
            file_path=str(file.relative_to(self.project_path)),
            inputs=inputs,
            outputs=outputs,
            injected_services=injected,
        )

    def _parse_service_file(self, file: Path) -> AngularServiceDigest | None:
        content = self._read(file)
        cls = re.search(r"export\s+class\s+([A-Za-z0-9_]+)", content)
        if not cls:
            return None
        calls = []
        for m in re.finditer(r"this\.http\.(get|post|put|delete|patch)\s*<*([^>(]*)>*\(([^)]+)\)", content):
            method, response_type, args = m.groups()
            url = args.split(",")[0].strip()
            calls.append(
                {
                    "method": method.upper(),
                    "url": url,
                    "payload_shape": "unknown",
                    "response_shape": response_type.strip() or "unknown",
                }
            )
        return AngularServiceDigest(
            name=cls.group(1),
            file_path=str(file.relative_to(self.project_path)),
            http_calls=calls,
            injected_dependencies=self._constructor_types(content),
        )

    def _parse_routes(self, file: Path) -> list[dict]:
        content = self._read(file)
        rows = []
        for path, component in re.findall(r"path\s*:\s*'([^']*)'[\s\S]*?component\s*:\s*([A-Za-z0-9_]+)", content):
            rows.append({"path": path, "component": component})
        for path, lazy in re.findall(r"path\s*:\s*'([^']*)'[\s\S]*?loadChildren\s*:\s*\(\)\s*=>\s*import\('([^']+)'\)", content):
            rows.append({"path": path, "component": f"lazy:{lazy}"})
        return rows

    @staticmethod
    def _constructor_types(content: str) -> list[str]:
        m = re.search(r"constructor\s*\(([\s\S]*?)\)", content)
        if not m:
            return []
        params = m.group(1)
        types = []
        for part in params.split(","):
            if ":" in part:
                types.append(part.split(":")[-1].strip().strip("?"))
        return [t for t in types if t]

    @staticmethod
    def _extract_env_urls(content: str) -> list[str]:
        values = []
        for key in ("apiUrl", "baseUrl", "apiBaseUrl"):
            m = re.search(rf"{key}\s*:\s*['\"]([^'\"]+)['\"]", content)
            if m:
                values.append(f"{key}:{m.group(1)}")
        return values

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
