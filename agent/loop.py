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
[CRITICAL — INDEX RETURNED NO USEFUL DATA FOR THIS QUESTION]

The search tools found nothing relevant. You MUST output ONLY the following and then STOP:

"No indexed information found for [repeat the module/class/feature name from the question].

The codebase index did not return relevant context for this query.
Possible reasons:
  - This module or class name may not be indexed (check spelling or try a different name)
  - The index may be out of date — rebuild with: POST /reindex

To get a real answer, try rephrasing with:
  - A specific Angular module name that is indexed
  - A Spring service class name (e.g. SomeServiceImpl)
  - A REST endpoint path (e.g. /api/v1/something)

I cannot describe or guess how this would work — I have no indexed evidence for this query."

DO NOT write class names. DO NOT write architecture layers. DO NOT describe how this might work.
STOP after the message above.
"""


# ------------------------------------------------------------------
# Synthesis prompts (mode-aware)
# ------------------------------------------------------------------

_SYNTHESIS_SUFFIX = {
    "chat": """\
Answer the developer's question using ONLY the gathered context above.

Before writing anything: scan the [Gathered N] blocks. If none contain relevant information
for the class/module/feature asked about, respond with:
  "No indexed information found for [name from question]. Try reindexing or rephrasing."
and STOP — do not describe how it might work.

Rules:
- Every class/method/endpoint name you write MUST appear verbatim in a [Gathered N] block
- If a detail is not in the context: write "not found in index" — never guess
- Forbidden words (mean you are hallucinating): likely, might, could, probably, assuming,
  typically, usually, generally, standard, "would be", "should be", "appears to"

For API / endpoint documentation questions, format your answer as:

  **Endpoint:** `[METHOD] [path]`
  **Controller:** ClassName (project)
  **Auth:** @EntitlementOrRoleBasedAuthorisation context or "none"
  **Request DTO:** ClassName → list fields with types
  **Response DTO:** ClassName → list fields with types

For DTO / data model questions, include a field table:
  | Field | Type | Required | Validation | Notes |
  |-------|------|----------|------------|-------|

For external/downstream service questions, include for each Feign client:
  **Client:** FeignClientName → resolved URL (from application.properties)
  **OAuth scope:** @AuthorizationToken scope value (if present)
  **Calls:**
    - [METHOD] path — Request: DTOName, Response: DTOName

For MongoDB collection / data model questions:
  **Collection:** name (from @Document annotation)
  | Field | Java Type | Required | DB Ref / Relationship |
  |-------|-----------|----------|-----------------------|\
""",

    "deep": """\
STEP 1 — EVIDENCE CHECK (do this before writing anything):
Scan every [Gathered N — ...] block above for class names, method names, or endpoints
that are directly related to the question.

If NONE of the gathered blocks contain relevant information for the module/feature/class
asked about, output EXACTLY this and then STOP — do not write any layers:

  "No indexed information found for [name from the question].
   The index returned no matching classes, endpoints, or services.
   Try reindexing (POST /reindex) or rephrase using a specific class or endpoint name."

STEP 2 — FORBIDDEN WORDS (self-check before every sentence):
These words mean you are guessing from training knowledge, not the index:
  likely · might · could · probably · assuming · assumed · expected · typical · typically
  usually · generally · standard · "in a standard" · "common pattern" · "would be" · "should be"
  "appears to" · "seems to" · "perhaps" · "presumably"

If you catch yourself about to write any forbidden word: STOP.
Either cite the actual source text with [SOURCE N], or write "Not found in index".

STEP 3 — WRITE LAYERS (only if Step 1 found relevant evidence):
For each layer, you MUST have a [Gathered N] block that explicitly names the class,
method, or value. If you cannot cite a source, write "Not found in index" for that layer.

Structure (only include layers that appear in the gathered context above):

1. Direct answer — name the exact classes and project found in the sources (2-3 sentences max)

2. Angular layer (only if a [Gathered N] block names this component/service)
   - Component name and file path from context
   - Angular service: HTTP method + URL exactly as shown in sources
   - Base class pattern if mentioned in sources

3. Controller layer (only if a [Gathered N] block names this controller and endpoint)
   - Controller class name and project exactly as in sources
   - Endpoint path exactly as in sources
   - Auth annotation context exactly as in sources
   - Request/Response DTO names exactly as in sources

4. Strategy/Delegate layer (only if strategyFactory or Delegate class appears in sources)

5. Service implementation (only if ServiceImpl class appears in sources)
   - Class name and private final field dependencies exactly as in sources
   - Method logic from method_body if present in sources — quote it, don't paraphrase

6. External service calls (only if Feign client names appear in sources)
   - Client class name → resolved URL → endpoint → request/response DTO — all from sources

7. Repository / Database (only if repository class or @Document collection appears in sources)
   - Class name, query method, collection name — all from sources

8. Kafka events (only if topic name or producer/consumer class appears in sources)

9. What was not found — list which layers had NO evidence in the gathered context\
""",

    "generate": """\
Produce implementation-ready output. Use EXACTLY this structure:

For DATA MODEL / API CONTRACT generation (when asked for docs, schemas, or OpenAPI):
  ### API Contract: [ServiceName]
  **Base URL:** <resolved from application.properties>

  #### [METHOD] /path
  **Auth:** @EntitlementOrRoleBasedAuthorisation context or none
  **Request Body:** DTOName
  | Field | Type | Required | Validation |
  |-------|------|----------|------------|
  **Response:** DTOName
  | Field | Type | Notes |
  |-------|------|-------|

  ### MongoDB Data Model
  **Collection:** name
  | Field | Java Type | @DBRef | Notes |
  |-------|-----------|--------|-------|

For CODE GENERATION (new files, modifications):
  For each file to MODIFY:
    ### FILE: <relative-path-from-project-root> [MODIFY]
    ```diff
    --- a/<relative-path>
    +++ b/<relative-path>
    @@ -<old-line>,<old-count> +<new-line>,<new-count> @@
     <3 context lines before>
    -<removed line>
    +<added line>
     <3 context lines after>
    ```

  For each NEW file:
    ### FILE: <relative-path-from-project-root> [CREATE]
    ```<java|typescript|sql>
    <complete file content>
    ```

Rules:
- Use exact package names, imports, annotations from gathered context
- Follow Lombok pattern: @RequiredArgsConstructor + private final fields (no explicit constructor)
- Use MongoRepository / MongoTemplate patterns, not JPA
- Use Feign client pattern with client-api.<name>.baseurl URL convention
- Use @AuthorizationToken for OAuth-secured Feign clients
- Match existing exception handling patterns (custom *FeignClientException types)
- After all FILE sections, add ## Impact summary listing downstream services/components impacted\
""",

    "impact": """\
Provide a structured impact analysis using ONLY information from the gathered context:

1. **Direct files impacted** — list each with specific reason from context
2. **Spring Boot services** — controllers, service beans, repositories, MongoDB documents affected
3. **Angular components and services** — pan-portal components and HTTP service calls affected
4. **API contract changes** — new/removed/modified endpoints, request/response shape changes
5. **MongoDB collection changes** — new collections, document field additions/removals, index changes
6. **Auth/security changes** — @EntitlementOrRoleBasedAuthorisation context changes, OAuth scope changes
7. **Kafka event changes** — topic name changes, EventModel payload changes, new producer/consumer
8. **Feign client changes** — URL changes, new downstream calls, DTO changes in Feign interfaces
9. **Risk level:** LOW / MEDIUM / HIGH — with specific justification from the indexed context

If the gathered context does not cover a layer above, state "Not enough context retrieved for [layer]".\
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


_STRUCTURAL_MISS_WARNING = """\
[NOTE: The graph traversal tools (describe_feature / trace_request / get_method_calls)
returned no specific information about the class or module asked about.
The context below comes from semantic search only and may be off-topic.
Apply the EVIDENCE CHECK strictly: if no [Gathered N] block explicitly names the
class/module from the question, output the "No indexed information found" message and stop.]
"""


def _has_structural_hits(tool_results: list[dict]) -> bool:
    """Return True if at least one structural tool returned meaningful content."""
    for tr in tool_results:
        if tr["tool"] not in _STRUCTURAL_TOOLS:
            continue
        result = tr["result"].strip().lower()
        if not result:
            continue
        if any(result.startswith(m) for m in _EMPTY_RESULT_MARKERS):
            continue
        if len(result) > 120:
            return True
    return False


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

    is_thin = _is_context_thin(tool_results)
    thin_warning = _THIN_CONTEXT_WARNING if is_thin else ""
    # For deep mode: if structural tools returned nothing, add a softer structural-miss note
    structural_warning = (
        _STRUCTURAL_MISS_WARNING
        if (not is_thin and mode == "deep" and not _has_structural_hits(tool_results))
        else ""
    )
    file_section = f"\nCurrently open file:\n{file_context}\n" if file_context else ""
    history_block = format_history(history)
    mode_suffix = _SYNTHESIS_SUFFIX.get(mode, _SYNTHESIS_SUFFIX["chat"])

    return (
        f"{SYSTEM_PROMPT_BASE}\n\n"
        f"{thin_warning}"
        f"{structural_warning}"
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

        # Isolate the synthesis LLM call — a failure here should NOT discard
        # the tool results or cause the caller to fall back to RAGChain.
        try:
            answer = str(self.llm.invoke(prompt))
        except Exception as exc:
            logger.warning("Synthesis LLM invoke failed: %s", exc)
            answer = (
                f"[Synthesis failed: {exc}]\n\n"
                "The tools ran successfully (see tools_used). "
                "Try the streaming endpoint /ask/stream for a more reliable response."
            )

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
