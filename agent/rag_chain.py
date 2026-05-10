from __future__ import annotations

import os
from collections.abc import Iterator

from dotenv import load_dotenv
from langchain_ollama import OllamaEmbeddings, OllamaLLM

from .prompts import PROMPT_CODE_GENERATION, PROMPT_CODE_QA, PROMPT_IMPACT_ANALYSIS, SYSTEM_PROMPT_BASE
from embeddings.vector_store import VectorStore


class RAGChain:
    def __init__(self):
        load_dotenv()
        self.model = os.getenv("OLLAMA_MODEL", "deepseek-coder-v2")
        self.embed_model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self.base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.llm = OllamaLLM(model=self.model, base_url=self.base_url)
        self.embeddings = OllamaEmbeddings(model=self.embed_model, base_url=self.base_url)
        self.store = VectorStore(os.getenv("CHROMA_PATH", "./vector_db"))

    def ask(self, question: str, mode: str, file_context: str | None = None) -> tuple[str, list[dict]]:
        qvec = self.embeddings.embed_query(question)
        hits = self.store.query(qvec, n_results=8)
        context = "\n\n".join(h["content"] for h in hits)
        prompt = self._render_prompt(mode, context, question, file_context or "")
        text = self.llm.invoke(prompt)
        return str(text), [h["metadata"] for h in hits]

    def stream_ask(self, question: str, mode: str, file_context: str | None = None) -> Iterator[str]:
        qvec = self.embeddings.embed_query(question)
        hits = self.store.query(qvec, n_results=8)
        context = "\n\n".join(h["content"] for h in hits)
        prompt = self._render_prompt(mode, context, question, file_context or "")
        for token in self.llm.stream(prompt):
            yield str(token)

    def get_retriever(self, filters: dict | None = None):
        def _retrieve(query: str):
            qvec = self.embeddings.embed_query(query)
            return self.store.query(qvec, n_results=8, filters=filters)

        return _retrieve

    @staticmethod
    def _render_prompt(mode: str, context: str, question: str, file_context: str) -> str:
        if mode == "generate":
            body = PROMPT_CODE_GENERATION.format(context=context, question=question, file_context=file_context)
        elif mode == "impact":
            body = PROMPT_IMPACT_ANALYSIS.format(context=context, question=question)
        else:
            body = PROMPT_CODE_QA.format(context=context, question=question)
        return f"{SYSTEM_PROMPT_BASE}\n\n{body}"
