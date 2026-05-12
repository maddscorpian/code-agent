from __future__ import annotations

import os
from collections.abc import Iterator

from dotenv import load_dotenv
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.llms import Ollama

from .prompts import (
    PROMPT_CODE_GENERATION,
    PROMPT_CODE_QA,
    PROMPT_DEEP_RESEARCH,
    PROMPT_IMPACT_ANALYSIS,
    SYSTEM_PROMPT_BASE,
    format_history,
)
from embeddings.vector_store import VectorStore


class RAGChain:
    def __init__(self):
        load_dotenv()
        self.model = os.getenv("OLLAMA_MODEL", "deepseek-coder-v2")
        self.embed_model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self.base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.llm = Ollama(model=self.model, base_url=self.base_url)
        self.embeddings = OllamaEmbeddings(model=self.embed_model, base_url=self.base_url)
        self.store = VectorStore(os.getenv("CHROMA_PATH", "./vector_db"))

    def ask(
        self,
        question: str,
        mode: str,
        file_context: str | None = None,
        history: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        hits = self._retrieve_with_depth(question, mode)
        context = self._format_context(hits)
        prompt = self._render_prompt(mode, context, question, file_context or "", history or [])
        text = self.llm.invoke(prompt)
        return str(text), hits

    def stream_ask(
        self,
        question: str,
        mode: str,
        file_context: str | None = None,
        history: list[dict] | None = None,
    ) -> Iterator[str]:
        hits = self._retrieve_with_depth(question, mode)
        context = self._format_context(hits)
        prompt = self._render_prompt(mode, context, question, file_context or "", history or [])
        for token in self.llm.stream(prompt):
            yield str(token)

    def get_retriever(self, filters: dict | None = None):
        def _retrieve(query: str):
            qvec = self.embeddings.embed_query(query)
            return self.store.query(qvec, n_results=8, filters=filters)
        return _retrieve

    @staticmethod
    def _render_prompt(
        mode: str,
        context: str,
        question: str,
        file_context: str,
        history: list[dict],
    ) -> str:
        history_block = format_history(history)
        if mode == "generate":
            body = PROMPT_CODE_GENERATION.format(
                history=history_block, context=context,
                question=question, file_context=file_context,
            )
        elif mode == "deep":
            body = PROMPT_DEEP_RESEARCH.format(
                history=history_block, context=context, question=question,
            )
        elif mode == "impact":
            body = PROMPT_IMPACT_ANALYSIS.format(
                history=history_block, context=context, question=question,
            )
        else:
            body = PROMPT_CODE_QA.format(
                history=history_block, context=context, question=question,
            )
        return f"{SYSTEM_PROMPT_BASE}\n\n{body}"

    def _retrieve_with_depth(self, question: str, mode: str) -> list[dict]:
        if mode != "deep":
            qvec = self.embeddings.embed_query(question)
            return self.store.query(qvec, n_results=12)

        # Deep mode: multi-query retrieval with deduplication
        query_variants = [
            question,
            f"Detailed architecture and flow for: {question}",
            f"Endpoints, entities, DTOs, and dependencies related to: {question}",
            f"Security, auth, and edge cases related to: {question}",
            f"Angular components and services related to: {question}",
        ]
        merged: dict[str, dict] = {}
        for q in query_variants:
            qvec = self.embeddings.embed_query(q)
            for hit in self.store.query(qvec, n_results=14):
                md = hit.get("metadata", {})
                key = (
                    f"{md.get('project', '')}::{md.get('file_path', '')}::"
                    f"{md.get('class_name', '')}::{md.get('method_name', '')}::"
                    f"{hash(hit.get('content', ''))}"
                )
                if key not in merged or hit.get("distance", 9e9) < merged[key].get("distance", 9e9):
                    merged[key] = hit
        return sorted(merged.values(), key=lambda h: h.get("distance", 9e9))[:30]

    @staticmethod
    def _format_context(hits: list[dict]) -> str:
        blocks = []
        for i, h in enumerate(hits, start=1):
            md = h.get("metadata", {})
            header = (
                f"[SOURCE {i}] project={md.get('project')} type={md.get('type')} "
                f"file={md.get('file_path', 'digest')} "
                f"class={md.get('class_name', '')} method={md.get('method_name', '')}"
            )
            blocks.append(f"{header}\n{h.get('content', '')}")
        return "\n\n".join(blocks)
