from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path


class PomParser:
    """Extracts build dependencies and DB migration summaries from Spring Boot projects."""

    def parse_dependencies(self, project_path: str) -> list[str]:
        root = Path(project_path)
        pom = root / "pom.xml"
        if pom.exists():
            return self._parse_pom(pom)
        for gradle_file in ("build.gradle", "build.gradle.kts"):
            g = root / gradle_file
            if g.exists():
                return self._parse_gradle(g)
        return []

    def parse_migrations(self, project_path: str) -> list[str]:
        """Scan Flyway/Liquibase migration files and return human-readable summaries."""
        root = Path(project_path)
        summaries: list[str] = []

        # Flyway: V{version}__{description}.sql
        for sql_file in sorted(root.rglob("V*.sql")):
            text = self._safe_read(sql_file)
            if not text:
                continue
            tables = re.findall(r"(?:CREATE|ALTER|DROP)\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)[`\"]?", text, re.IGNORECASE)
            columns = re.findall(r"ADD\s+(?:COLUMN\s+)?[`\"]?(\w+)[`\"]?\s+(\w+)", text, re.IGNORECASE)
            summary_parts = [f"file={sql_file.name}"]
            if tables:
                summary_parts.append(f"tables={','.join(dict.fromkeys(tables))}")
            if columns:
                summary_parts.append(f"add_columns={','.join(f'{c}:{t}' for c, t in columns[:6])}")
            summaries.append(" ".join(summary_parts))

        # Liquibase: changelog files
        for xml_file in root.rglob("db.changelog*.xml"):
            text = self._safe_read(xml_file)
            if not text:
                continue
            tables = re.findall(r'tableName="(\w+)"', text)
            if tables:
                summaries.append(f"file={xml_file.name} tables={','.join(dict.fromkeys(tables))}")

        return summaries

    @staticmethod
    def _parse_pom(pom_path: Path) -> list[str]:
        try:
            tree = ET.parse(pom_path)
            ns = {"m": "http://maven.apache.org/POM/4.0.0"}
            deps: list[str] = []
            for dep in tree.findall(".//m:dependency", ns):
                group = dep.findtext("m:groupId", namespaces=ns) or ""
                artifact = dep.findtext("m:artifactId", namespaces=ns) or ""
                version = dep.findtext("m:version", namespaces=ns) or ""
                scope = dep.findtext("m:scope", namespaces=ns) or "compile"
                if group and artifact:
                    entry = f"{group}:{artifact}"
                    if version:
                        entry += f":{version}"
                    if scope != "compile":
                        entry += f" [{scope}]"
                    deps.append(entry)
            return deps
        except Exception:
            return []

    @staticmethod
    def _parse_gradle(gradle_path: Path) -> list[str]:
        try:
            text = gradle_path.read_text(encoding="utf-8", errors="ignore")
            deps: list[str] = []
            pattern = re.compile(
                r"""(?:implementation|api|runtimeOnly|testImplementation|compileOnly)\s*['"]([\w.\-:]+)['"]""",
                re.MULTILINE,
            )
            for m in pattern.finditer(text):
                deps.append(m.group(1))
            return deps
        except Exception:
            return []

    @staticmethod
    def _safe_read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
