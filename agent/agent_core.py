from __future__ import annotations

import logging
from collections.abc import Iterator

from .rag_chain import RAGChain

logger = logging.getLogger(__name__)


class AgentCore:
    """
    Orchestrates the agent loop.

    Primary path  : AgentLoop (Plan → Gather → Synthesize)
    Fallback path : RAGChain single-shot RAG (used if loop init fails)
    """

    def __init__(self):
        self.rag = RAGChain()
        self._loop = None
        self._init_loop()

    def _init_loop(self) -> None:
        try:
            from langchain_community.llms import Ollama
            from .loop import AgentLoop
            from .tools import build_tools_map

            # Planner: small context (output is short JSON), fast response
            planner_llm = Ollama(
                model=self.rag.model,
                base_url=self.rag.base_url,
                num_ctx=4096,
                temperature=0.1,
            )
            # Synthesizer: larger context for gathered tool results
            synth_llm = Ollama(
                model=self.rag.model,
                base_url=self.rag.base_url,
                num_ctx=8192,
                temperature=0.15,
                top_p=0.95,
            )
            self._loop = AgentLoop(llm=synth_llm, planner_llm=planner_llm, tools_map=build_tools_map())
            logger.info("AgentLoop initialised (model=%s)", self.rag.model)
        except Exception as exc:
            logger.warning("AgentLoop init failed (%s) — falling back to RAGChain only", exc)
            self._loop = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        question: str,
        mode: str | None = None,
        file_context: str | None = None,
        history: list[dict] | None = None,
    ) -> dict:
        actual_mode = mode or self.detect_mode(question)
        if self._loop is not None:
            try:
                return self._loop.run(question, actual_mode, file_context or "", history or [])
            except Exception as exc:
                logger.warning("AgentLoop.run failed (%s) — falling back to RAGChain", exc)
        answer, sources = self.rag.ask(question, actual_mode, file_context, history)
        return {"answer": answer, "sources": sources, "mode": actual_mode}

    def stream_run(
        self,
        question: str,
        mode: str | None = None,
        file_context: str | None = None,
        history: list[dict] | None = None,
    ) -> Iterator[str]:
        actual_mode = mode or self.detect_mode(question)
        if self._loop is not None:
            try:
                yield from self._loop.stream_run(
                    question, actual_mode, file_context or "", history or []
                )
                return
            except Exception as exc:
                logger.warning("AgentLoop.stream_run failed (%s) — falling back to RAGChain", exc)
        yield from self.rag.stream_ask(question, actual_mode, file_context, history)

    # ------------------------------------------------------------------
    # Mode detection (unchanged)
    # ------------------------------------------------------------------

    @staticmethod
    def detect_mode(question: str) -> str:
        q = question.lower()
        if any(w in q for w in (
            "deep", "detailed", "thorough", "root cause", "all impacted",
            "comprehensive", "explain in detail", "walk me through",
        )):
            return "deep"
        if any(w in q for w in (
            "add", "create", "implement", "write", "build",
            "modify", "update", "generate", "refactor",
        )):
            return "generate"
        if any(w in q for w in (
            "impact", "affect", "change", "what happens", "risk", "break", "if i",
        )):
            return "impact"
        return "chat"
