from __future__ import annotations

import logging
from collections.abc import Iterator

from .planner import PlanResult, Planner, ToolCall
from .prompts import SYSTEM_PROMPT_BASE, format_history

logger = logging.getLogger(__name__)

# Sentinel prefix used to pass plan metadata through the stream without a callback
_PLAN_SENTINEL = "__PLAN__"
_PLAN_SENTINEL_END = "__END_PLAN__"

# Cap tool output — search_deep returns up to 30 chunks so needs more room
_MAX_TOOL_OUTPUT = 6000


# ------------------------------------------------------------------
# Synthesis prompts (mode-aware)
# ------------------------------------------------------------------

_SYNTHESIS_SUFFIX = {
    "chat": """\
Answer the developer's question precisely. Reference actual class names, file paths, \
and method names from the gathered context. If context is insufficient, say so.\
""",

    "deep": """\
Provide a deep, evidence-backed answer. Cite [SOURCE N] for every specific claim.

Structure your answer as:
1. **Direct answer** — 2-3 sentences summarising what happens
2. **Layer-by-layer trace** — walk each layer explicitly, citing sources:
   - Angular: component → service → HTTP call (method, URL)
   - Controller: class name, handler method, auth/roles
   - Service: class, key methods, what they call (use method_call_graph data if available)
   - Repository: class, relevant query methods or @Query SQL/JPQL
   - Entity/DB: entity class, table, relevant fields
   - Events: Kafka/RabbitMQ topics produced or consumed
3. **Method call chain** — list the concrete call sequence step by step if method_call_graph data is present
4. **Cross-service interactions** — Feign clients, external calls, downstream services triggered
5. **Failure modes** — what can fail at each layer and what the impact would be
6. **Security** — auth requirements, roles, JWT claims involved
7. **Context gaps** — explicitly state which layers you could NOT find context for

Rules:
- Cite [SOURCE N] for every class name, method, or endpoint you reference
- If method_call_graph data is in the sources, use it to trace the actual call chain
- If a layer is missing from context, write "Context not retrieved for [layer]"
- Never invent class names, method names, or endpoints not present in the sources\
""",

    "generate": """\
Produce implementation-ready output. Use EXACTLY this structure so the output can be applied automatically:

For each file to MODIFY (existing file — add/change methods, fields, annotations):
  ### FILE: <relative-path-from-project-root> [MODIFY]
  ```diff
  --- a/<relative-path>
  +++ b/<relative-path>
  @@ -<old-line>,<old-count> +<new-line>,<new-count> @@
   <3 context lines before change>
  -<line being removed>
  +<line being added>
   <3 context lines after change>
  ```

For each NEW file to CREATE:
  ### FILE: <relative-path-from-project-root> [CREATE]
  ```<java|typescript|sql>
  <complete file content>
  ```

Rules:
- Use the exact package names, imports, annotations, and naming from the gathered context
- Include at least 3 context lines (space-prefixed) around each diff hunk
- Generate a matching test class in src/test/… for any new service/controller method
- After all FILE sections, add a short ## Impact summary listing downstream services/components
- For any new endpoint: also add the matching Flyway migration if a new column or table is needed\
""",

    "impact": """\
Provide a structured impact analysis:
1. Direct files impacted (list each with reason)
2. Spring Boot services impacted (controllers, service beans, repositories, entities)
3. Angular components and services impacted
4. API contract changes (new/removed endpoints, request/response shape changes)
5. DB schema changes and required migrations
6. Auth/security changes (roles, permit-all rules, JWT claims)
7. Kafka/RabbitMQ event schema or topic changes
8. Risk level: LOW / MEDIUM / HIGH — with specific justification\
""",
}


def _synthesis_prompt(
    question: str,
    mode: str,
    tool_results: list[dict],
    file_context: str,
    history: list[dict],
) -> str:
    # Build gathered context block
    context_blocks: list[str] = []
    for i, tr in enumerate(tool_results, start=1):
        header = f"[Gathered {i}: {tr['tool']}({tr['input']!r})]"
        result = tr["result"]
        if len(result) > _MAX_TOOL_OUTPUT:
            result = result[:_MAX_TOOL_OUTPUT] + "\n… (truncated)"
        context_blocks.append(f"{header}\n{result}")

    context_section = "\n\n".join(context_blocks) if context_blocks else "(no context gathered)"

    file_section = f"\nCurrently open file:\n{file_context}\n" if file_context else ""
    history_block = format_history(history)
    mode_suffix = _SYNTHESIS_SUFFIX.get(mode, _SYNTHESIS_SUFFIX["chat"])

    return (
        f"{SYSTEM_PROMPT_BASE}\n\n"
        f"Gathered context from codebase tools:\n{context_section}\n"
        f"{file_section}"
        f"{history_block}"
        f"Developer question: {question}\n\n"
        f"{mode_suffix}"
    )


# ------------------------------------------------------------------
# AgentLoop
# ------------------------------------------------------------------

class AgentLoop:
    """
    Plan → Gather → Synthesize agent loop.

    Replaces the brittle ReAct agent with a two-LLM-call design:
      1. Planner LLM call: decide which tools to invoke
      2. Execute tools (all local/fast — no LLM involved)
      3. Synthesizer LLM call: produce final answer from gathered context
    """

    def __init__(self, llm, tools_map: dict):
        self.llm = llm
        self.tools_map = tools_map
        self.planner = Planner(llm)

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    def run(
        self,
        question: str,
        mode: str,
        file_context: str = "",
        history: list[dict] | None = None,
    ) -> dict:
        plan = self.planner.plan(question, mode, history or [])
        logger.info("Plan reasoning: %s | tools: %s",
                    plan.reasoning, [tc.tool for tc in plan.tool_calls])

        tool_results = self._execute(plan.tool_calls)
        prompt = _synthesis_prompt(question, mode, tool_results, file_context, history or [])
        answer = str(self.llm.invoke(prompt))

        return {
            "answer": answer,
            "mode": mode,
            "plan_reasoning": plan.reasoning,
            "tools_used": [{"tool": tr["tool"], "input": tr["input"]} for tr in tool_results],
            "sources": [],
        }

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def stream_run(
        self,
        question: str,
        mode: str,
        file_context: str = "",
        history: list[dict] | None = None,
    ) -> Iterator[str]:
        """
        Generator that yields tokens.
        The very first yielded value is a plan sentinel so the caller can emit
        an SSE `event: plan` before the synthesis tokens begin.

        Sentinel format:
            __PLAN__<json_string>__END_PLAN__
        """
        import json

        plan = self.planner.plan(question, mode, history or [])
        logger.info("Plan (stream) reasoning: %s | tools: %s",
                    plan.reasoning, [tc.tool for tc in plan.tool_calls])

        tool_results = self._execute(plan.tool_calls)

        # Emit plan metadata as the first "token" — server converts to SSE event
        plan_data = {
            "reasoning": plan.reasoning,
            "tools": [tc.tool for tc in plan.tool_calls],
        }
        yield f"{_PLAN_SENTINEL}{json.dumps(plan_data)}{_PLAN_SENTINEL_END}"

        prompt = _synthesis_prompt(question, mode, tool_results, file_context, history or [])
        for token in self.llm.stream(prompt):
            yield str(token)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute(self, tool_calls: list[ToolCall]) -> list[dict]:
        results: list[dict] = []
        for tc in tool_calls:
            fn = self.tools_map.get(tc.tool)
            if fn is None:
                logger.warning("Unknown tool %r — skipping", tc.tool)
                continue
            try:
                raw = fn(tc.input)
                result = str(raw).strip()
                logger.debug("Tool %s(%r) → %d chars", tc.tool, tc.input, len(result))
            except Exception as exc:
                result = f"[Tool error: {exc}]"
                logger.warning("Tool %s failed: %s", tc.tool, exc)
            results.append({"tool": tc.tool, "input": tc.input, "result": result})
        return results
