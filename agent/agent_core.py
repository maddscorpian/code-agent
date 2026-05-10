from __future__ import annotations

from langchain.agents import AgentType, initialize_agent
from langchain_ollama import OllamaLLM

from .rag_chain import RAGChain
from .tools import build_tools


class AgentCore:
    def __init__(self):
        self.rag = RAGChain()
        self.tools = build_tools()
        self.react = initialize_agent(
            tools=self.tools,
            llm=OllamaLLM(model=self.rag.model, base_url=self.rag.base_url),
            agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
            verbose=False,
            handle_parsing_errors=True,
        )

    def run(self, question: str, mode: str | None, file_context: str | None = None) -> dict:
        actual_mode = mode or self.detect_mode(question)
        if actual_mode == "impact":
            answer = self.react.run(question)
            sources = []
        else:
            answer, sources = self.rag.ask(question, actual_mode, file_context=file_context)
        return {"answer": answer, "sources": sources, "mode": actual_mode}

    @staticmethod
    def detect_mode(question: str) -> str:
        q = question.lower()
        if any(w in q for w in ("add", "create", "implement", "write", "build", "modify", "update")):
            return "generate"
        if any(w in q for w in ("impact", "affect", "change", "what happens", "risk")):
            return "impact"
        return "chat"
