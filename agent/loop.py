from __future__ import annotations

import logging
from collections.abc import Iterator

from .planner import PlanResult, Planner, ToolCall
from .prompts import SYSTEM_PROMPT_BASE, format_history

logger = logging.getLogger(__name__)

# Sentinel prefix used to pass plan metadata through the stream without a callback
_PLAN_SENTINEL = "__PLAN__"
_PLAN_SENTINEL_END = "__END_PLAN__"

# Progress sentinels — emitted before each phase so the server can send event: progress
_PROGRESS_SENTINEL = "__PROGRESS__"
_PROGRESS_SENTINEL_END = "__END_PROGRESS__"

# Cap tool output — raised to 12000 to preserve more of search_deep's 30-chunk results
_MAX_TOOL_OUTPUT = 12000

# Fingerprint length used for cross-tool deduplication
_DEDUP_FINGERPRINT_LEN = 200

# Structural/graph tools produce verified class chains — higher signal than semantic search.
# Placed first in the synthesis context so the LLM anchors on them before softer evidence.
_STRUCTURAL_TOOLS = frozenset({
    "describe_feature", "trace_request", "get_method_calls",
    "impact_graph", "find_callers", "trace_event_flow",
})

# Strings that indicate a tool returned nothing useful
_EMPTY_RESULT_MARKERS = (
    "not found",
    "no results",
    "no nodes",
    "no matching",
    "graph not built",
    "no graph",
    "error:",
    "[tool error",
    "(no context",
)

_THIN_CONTEXT_WARNING = """\
[CONTEXT WARNING: The codebase index returned very little or no information for this question.
Do NOT answer from general knowledge. State clearly which specific things were not found in the
index and stop. Do not guess, infer, or describe how this type of system typically works.]
"""


# ------------------------------------------------------------------
# Synthesis prompts (mode-aware)
# ------------------------------------------------------------------

_SYNTHESIS_SUFFIX = {
    "chat": """\
Answer the developer's question using ONLY the gathered context above.
- Reference actual class names, file paths, and method names exactly as they appear in the context.
- Do not use general knowledge about Angular or Spring Boot to fill in missing details.
- If a specific class, method, endpoint, or field is not in the gathered context, say it was not found in the index — do not guess.
- If the context is thin or off-topic, say so and suggest reindexing or rephrasing.\
""",

    "deep": """\
Provide a deep, evidence-backed answer using ONLY the gathered context. Cite [SOURCE N] for every specific claim.

Structure your answer using these layers (only include layers that appear in the context):

1. **Direct answer** — 2-3 sentences naming the exact classes involved
2. **Angular layer** — component name → Angular service → HTTP call (method + URL). Note if call goes through a base service class (override loadMany/loadOne pattern).
3. **Controller layer** — controller class, project, endpoint path, @EntitlementOrRoleBasedAuthorisation context if present, request/response DTOs
4. **Strategy/Delegate layer** — if strategyFactory.getStrategy() pattern present, name the Delegate class that handles the request
5. **Service implementation** — ServiceImpl class, its Lombok @RequiredArgsConstructor dependencies (private final fields), which Feign clients it calls (client-api.xxx.baseurl URL), @AuthorizationToken OAuth scope per downstream call, custom exception types
6. **Repository/Database layer** — MongoRepository derived method or MongoTemplate Criteria query, @Document collection name (with resolved db.collection.suffix)
7. **Kafka events** — topic from spring.kafka.consumer.topic property, EventModel event types dispatched, producer/consumer class names
8. **Cross-service interactions** — all Feign clients called, their resolved URLs, downstream services triggered
9. **Context gaps** — explicitly state which layers you could NOT find context for

Rules:
- Cite [SOURCE N] for every class name, method name, or endpoint you reference
- If method_body or method_call_graph data is in the sources, use it to describe actual logic
- If a layer is missing, write "Context not retrieved for [layer] — rephrase or reindex"
- Never invent class names, method names, or property values not present in the sources\
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


def _is_context_thin(tool_results: list[dict]) -> bool:
    """Return True when all tools returned empty or error-like results."""
    if not tool_results:
        return True
    useful_chars = 0
    for tr in tool_results:
        result_lower = tr["result"].lower().strip()
        is_empty = (
            not result_lower
            or any(result_lower.startswith(m) for m in _EMPTY_RESULT_MARKERS)
            or len(result_lower) < 80
        )
        if not is_empty:
            useful_chars += len(tr["result"])
    return useful_chars < 200


def _synthesis_prompt(
    question: str,
    mode: str,
    tool_results: list[dict],
    file_context: str,
    history: list[dict],
) -> str:
    # Build gathered context — structural tools first, cross-tool deduplication applied
    context_blocks: list[str] = []
    seen_fingerprints: set[str] = set()
    sorted_results = sorted(
        enumerate(tool_results, start=1),
        key=lambda x: (0 if x[1]["tool"] in _STRUCTURAL_TOOLS else 1),
    )
    for i, tr in sorted_results:
        result = tr["result"]
        if len(result) > _MAX_TOOL_OUTPUT:
            result = result[:_MAX_TOOL_OUTPUT] + "\n… (truncated)"
        # Deduplicate on paragraph boundaries — skip segments already seen from earlier tools
        segments = result.split("\n\n")
        unique_segments: list[str] = []
        for seg in segments:
            fp = seg.strip()[:_DEDUP_FINGERPRINT_LEN]
            if fp and fp not in seen_fingerprints:
                seen_fingerprints.add(fp)
                unique_segments.append(seg)
        if not unique_segments:
            continue
        signal_type = "STRUCTURAL" if tr["tool"] in _STRUCTURAL_TOOLS else "SEMANTIC"
        header = f"[Gathered {i} — {signal_type}: {tr['tool']}({tr['input']!r})]"
        context_blocks.append(f"{header}\n" + "\n\n".join(unique_segments))

    context_section = "\n\n".join(context_blocks) if context_blocks else "(no context gathered)"

    thin_warning = _THIN_CONTEXT_WARNING if _is_context_thin(tool_results) else ""
    file_section = f"\nCurrently open file:\n{file_context}\n" if file_context else ""
    history_block = format_history(history)
    mode_suffix = _SYNTHESIS_SUFFIX.get(mode, _SYNTHESIS_SUFFIX["chat"])

    return (
        f"{SYSTEM_PROMPT_BASE}\n\n"
        f"{thin_warning}"
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

    def __init__(self, llm, tools_map: dict, planner_llm=None):
        self.llm = llm
        self.tools_map = tools_map
        self.planner = Planner(planner_llm if planner_llm is not None else llm)

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
        Generator that yields tokens interleaved with progress and plan sentinels.

        Sentinel formats (parsed by server.py, never shown to user):
            __PROGRESS__{"stage":"planning"}__END_PROGRESS__
            __PROGRESS__{"stage":"tool","tool":"search_deep","n":1,"total":3}__END_PROGRESS__
            __PROGRESS__{"stage":"synthesizing"}__END_PROGRESS__
            __PLAN__{"reasoning":"...","tools":[...]}__END_PLAN__
        """
        import json
        history = history or []

        # ── Phase 1: Planning ─────────────────────────────────────────────
        yield f"{_PROGRESS_SENTINEL}{json.dumps({'stage': 'planning'})}{_PROGRESS_SENTINEL_END}"
        plan = self.planner.plan(question, mode, history)
        logger.info("Plan (stream) reasoning: %s | tools: %s",
                    plan.reasoning, [tc.tool for tc in plan.tool_calls])

        # ── Phase 2: Tool execution (inline — progress emitted per tool) ──
        tool_results: list[dict] = []
        total = len(plan.tool_calls)
        for idx, tc in enumerate(plan.tool_calls, start=1):
            yield f"{_PROGRESS_SENTINEL}{json.dumps({'stage': 'tool', 'tool': tc.tool, 'n': idx, 'total': total})}{_PROGRESS_SENTINEL_END}"
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
            tool_results.append({"tool": tc.tool, "input": tc.input, "result": result})

        # ── Emit plan strip (existing behaviour) ──────────────────────────
        plan_data = {"reasoning": plan.reasoning, "tools": [tc.tool for tc in plan.tool_calls]}
        yield f"{_PLAN_SENTINEL}{json.dumps(plan_data)}{_PLAN_SENTINEL_END}"

        # ── Phase 3: Synthesis ────────────────────────────────────────────
        yield f"{_PROGRESS_SENTINEL}{json.dumps({'stage': 'synthesizing'})}{_PROGRESS_SENTINEL_END}"
        prompt = _synthesis_prompt(question, mode, tool_results, file_context, history)
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
