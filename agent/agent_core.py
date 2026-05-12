from __future__ import annotations

try:
    from langchain.agents import AgentType, initialize_agent
except ImportError:
    from langchain.agents import initialize_agent
    try:
        from langchain.agents.agent_types import AgentType
    except ImportError:
        AgentType = None
from langchain_community.llms import Ollama

from .rag_chain import RAGChain
from .tools import build_tools


class AgentCore:
    def __init__(self):
        self.rag = RAGChain()
        self.tools = build_tools()
        self.react = None
        if AgentType is not None:
            try:
                self.react = initialize_agent(
                    tools=self.tools,
                    llm=Ollama(model=self.rag.model, base_url=self.rag.base_url),
                    agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
                    verbose=False,
                    handle_parsing_errors=True,
                )
            except Exception:
                self.react = None

    def run(
        self,
        question: str,
        mode: str | None,
        file_context: str | None = None,
        history: list[dict] | None = None,
    ) -> dict:
        actual_mode = mode or self.detect_mode(question)
        if actual_mode == "impact" and self.react is not None:
            try:
                answer = self.react.run(question)
            except Exception:
                answer, _ = self.rag.ask(question, "impact", file_context=file_context, history=history)
            sources = []
        else:
            answer, sources = self.rag.ask(question, actual_mode, file_context=file_context, history=history)
        return {"answer": answer, "sources": sources, "mode": actual_mode}

    @staticmethod
    def detect_mode(question: str) -> str:
        q = question.lower()
        if any(w in q for w in ("deep", "detailed", "thorough", "root cause", "all impacted", "comprehensive", "explain in detail", "walk me through")):
            return "deep"
        if any(w in q for w in ("add", "create", "implement", "write", "build", "modify", "update", "generate", "refactor")):
            return "generate"
        if any(w in q for w in ("impact", "affect", "change", "what happens", "risk", "break", "if i")):
            return "impact"
        return "chat"
