from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import tiktoken

logger = logging.getLogger(__name__)


class Chunker:
    def __init__(self, root: str):
        self.root = Path(root)
        self.digests_dir = self.root / "digests"
        self.encoder = None
        try:
            self.encoder = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:
            # Keep indexing usable in strict corporate/offline environments.
            logger.warning("tiktoken cl100k_base unavailable; using char-based chunking fallback: %s", exc)

    def build_chunks(self) -> list[dict]:
        chunks: list[dict] = []
        chunks.extend(self._digest_chunks())
        chunks.extend(self._code_chunks())
        return chunks

    def _digest_chunks(self) -> list[dict]:
        rows: list[dict] = []
        for file in self.digests_dir.glob("*.digest.json"):
            data = json.loads(file.read_text(encoding="utf-8"))
            project = data.get("project") or data.get("system", "master")
            if "endpoints" in data:
                for i, ep in enumerate(data.get("endpoints", [])):
                    content = (
                        f"Endpoint {ep.get('method')} {ep.get('path')}\n"
                        f"controller={ep.get('controller')} handler={ep.get('handler')}\n"
                        f"request={ep.get('request_dto')} response={ep.get('response_dto')}\n"
                        f"auth={ep.get('auth_required')} roles={ep.get('roles')}"
                    )
                    rows.append(self._chunk_dict(project, str(file), i, content, {"source": "digest", "project": project, "type": "endpoint", "name": ep.get("handler", "endpoint")}))
                for i, ent in enumerate(data.get("entities", [])):
                    content = f"Entity {ent.get('name')} table={ent.get('table')}\nfields={ent.get('fields')}\nrelationships={ent.get('relationships')}"
                    rows.append(self._chunk_dict(project, str(file), 1000 + i, content, {"source": "digest", "project": project, "type": "entity", "name": ent.get("name", "entity")}))
                for i, fc in enumerate(data.get("feign_clients", [])):
                    content = f"Feign {fc.get('client_name')} target={fc.get('target_service')}\ncalls={fc.get('calls')}"
                    rows.append(self._chunk_dict(project, str(file), 2000 + i, content, {"source": "digest", "project": project, "type": "feign", "name": fc.get("client_name", "feign")}))
            if file.name == "master.digest.json":
                auth = data.get("auth_flow", {})
                rows.append(self._chunk_dict("master", str(file), 0, f"Auth flow summary: {auth}", {"source": "digest", "project": "master", "type": "auth_flow", "name": "auth_flow"}))
        return rows

    def _code_chunks(self) -> list[dict]:
        rows = []
        for file in self.root.rglob("*"):
            if not file.is_file():
                continue
            if any(part.startswith(".") for part in file.parts):
                continue
            suffix = file.suffix.lower()
            if suffix not in {".java", ".ts", ".html", ".yml", ".yaml", ".properties", ".json"}:
                continue
            if "digests" in file.parts or "vector_db" in file.parts or "vscode-extension" in file.parts:
                continue
            rel = str(file.relative_to(self.root))
            project = rel.split("/")[0] if "/" in rel else "local-ai-agent"
            content = file.read_text(encoding="utf-8", errors="ignore")
            for idx, piece in enumerate(self._split_content(content)):
                rows.append(
                    self._chunk_dict(
                        project,
                        rel,
                        idx,
                        piece,
                        {"source": "code", "project": project, "type": suffix.lstrip("."), "file_path": rel, "class_name": self._guess_class(piece), "method_name": self._guess_method(piece)},
                    )
                )
        return rows

    def _split_content(self, content: str, target_tokens: int = 500, overlap_tokens: int = 50) -> list[str]:
        if self.encoder is not None:
            toks = self.encoder.encode(content)
            if len(toks) <= 600:
                return [content]
            chunks = []
            start = 0
            while start < len(toks):
                end = min(start + target_tokens, len(toks))
                chunk_toks = toks[start:end]
                chunks.append(self.encoder.decode(chunk_toks))
                if end == len(toks):
                    break
                start = max(0, end - overlap_tokens)
            return chunks

        # Approximate token size fallback: ~4 chars/token.
        target_chars = target_tokens * 4
        overlap_chars = overlap_tokens * 4
        if len(content) <= int(600 * 4):
            return [content]
        chunks = []
        start = 0
        while start < len(content):
            end = min(start + target_chars, len(content))
            chunks.append(content[start:end])
            if end == len(content):
                break
            start = max(0, end - overlap_chars)
        return chunks

    @staticmethod
    def _guess_class(content: str) -> str:
        import re

        m = re.search(r"(?:class|interface|enum)\s+([A-Za-z0-9_]+)", content)
        return m.group(1) if m else ""

    @staticmethod
    def _guess_method(content: str) -> str:
        import re

        m = re.search(r"(public|private|protected)\s+[A-Za-z0-9_<>\[\]]+\s+([A-Za-z0-9_]+)\s*\(", content)
        return m.group(2) if m else ""

    @staticmethod
    def _chunk_dict(project: str, file_path: str, idx: int, content: str, metadata: dict[str, Any]) -> dict:
        return {"id": f"{project}::{file_path}::{idx}", "content": content, "metadata": metadata}
