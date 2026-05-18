from __future__ import annotations

import os
import re
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
        self.llm = Ollama(
            model=self.model,
            base_url=self.base_url,
            num_ctx=8192,
            temperature=0.15,
            top_p=0.95,
        )
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

        # Change 3: targeted query variants covering different knowledge layers
        variants = self._build_deep_query_variants(question)
        merged: dict[str, dict] = {}
        for q in variants:
            qvec = self.embeddings.embed_query(q)
            for hit in self.store.query(qvec, n_results=14):
                key = self._hit_key(hit)
                if key not in merged or hit.get("distance", 9e9) < merged[key].get("distance", 9e9):
                    merged[key] = hit

        # Change 1: multi-hop — follow class names found in initial results
        merged = self._multihop_retrieve(list(merged.values()), merged)

        # Change 2: re-rank by question word overlap before capping
        reranked = self._rerank(list(merged.values()), question)
        return reranked[:50]

    # ------------------------------------------------------------------
    # Change 3: targeted deep-mode query variants
    # ------------------------------------------------------------------

    @staticmethod
    def _build_deep_query_variants(question: str) -> list[str]:
        variants = [
            question,
            f"service method implementation business logic: {question}",
            f"REST endpoint controller handler DTO request response: {question}",
            f"entity repository database query schema: {question}",
            f"configuration properties Feign Kafka event publisher: {question}",
            f"Angular component service HTTP call frontend: {question}",
            f"method call graph dependencies injection: {question}",
            f"security authentication authorization @PreAuthorize JWT role: {question}",
            f"exception error handling @ControllerAdvice fault tolerance: {question}",
            f"Kafka event topic property configuration value: {question}",
        ]
        # Targeted variant using service name if present
        svc = re.search(r'ms-java-[\w-]+|module-java-[\w-]+', question)
        if svc:
            variants.append(f"{svc.group(0)} implementation details: {question}")
        # Targeted variant using PascalCase class name if present
        cls = re.search(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', question)
        if cls:
            variants.append(f"{cls.group(1)} method calls dependencies implementation")
        return variants[:10]

    # ------------------------------------------------------------------
    # Change 1: multi-hop retrieval
    # ------------------------------------------------------------------

    def _multihop_retrieve(
        self, initial_hits: list[dict], merged: dict[str, dict]
    ) -> dict[str, dict]:
        """
        Extract class/bean names from initial hits and do follow-up targeted searches.
        Follows the call chain one hop deeper without infinite recursion.
        """
        found_names: set[str] = set()
        for hit in initial_hits:
            content = hit.get("content", "")
            # PascalCase class names (OrderService, CustomerRepository, etc.)
            found_names.update(re.findall(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', content))
            # Bean names in "dependencies=[...]" lines
            found_names.update(re.findall(r"'([A-Z][a-zA-Z]+)'", content))

        # Limit follow-up to avoid excessive embedding calls
        candidates = [n for n in found_names if n not in {"Optional", "ResponseEntity",
                      "List", "Map", "String", "Long", "Integer", "Boolean"}][:10]

        for name in candidates:
            qvec = self.embeddings.embed_query(
                f"{name} implementation method calls dependencies"
            )
            for hit in self.store.query(qvec, n_results=5):
                key = self._hit_key(hit)
                if key not in merged:
                    merged[key] = hit

        return merged

    # ------------------------------------------------------------------
    # Change 2: re-ranking by question word relevance
    # ------------------------------------------------------------------

    @staticmethod
    def _rerank(hits: list[dict], question: str) -> list[dict]:
        """
        Score and re-sort hits so the most question-relevant chunks appear first.
        The LLM's attention is strongest on early context, so this matters.
        """
        stop = {"what", "how", "does", "the", "and", "for", "with", "that", "this",
                "which", "when", "where", "from", "into", "about", "have", "been",
                "will", "can", "are", "its", "our", "their", "there", "here"}
        q_words = {w.lower() for w in re.findall(r'\b\w{4,}\b', question.lower())
                   if w.lower() not in stop}

        def score(hit: dict) -> float:
            md = hit.get("metadata", {})
            searchable = (hit.get("content", "") + " " + str(md)).lower()
            word_score = sum(1 for w in q_words if w in searchable)
            # Structured knowledge ranks above raw code for deep questions
            src_boost = {"digest": 1.5, "graph": 1.2, "code": 0.0}.get(md.get("source", "code"), 0.0)
            # Chunk types most useful for deep understanding
            type_boost = {
                "method_call_graph": 1.0, "endpoint": 0.8, "bean": 0.6,
                "entity": 0.5, "feign": 0.5, "api_contracts": 0.4,
            }.get(md.get("type", ""), 0.0)
            # Closer distance = higher relevance
            dist_score = max(0.0, 1.0 - hit.get("distance", 1.0))
            return word_score + src_boost + type_boost + dist_score

        return sorted(hits, key=score, reverse=True)

    @staticmethod
    def _hit_key(hit: dict) -> str:
        md = hit.get("metadata", {})
        return (
            f"{md.get('project', '')}::{md.get('file_path', '')}::"
            f"{md.get('class_name', '')}::{md.get('method_name', '')}::"
            f"{hash(hit.get('content', ''))}"
        )

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
