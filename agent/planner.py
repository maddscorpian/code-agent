from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Data types
# ------------------------------------------------------------------

@dataclass
class ToolCall:
    tool: str
    input: str


@dataclass
class PlanResult:
    reasoning: str
    tool_calls: list[ToolCall] = field(default_factory=list)


# ------------------------------------------------------------------
# Tool catalogue shown to the planner LLM
# ------------------------------------------------------------------

TOOL_CATALOGUE = """\
Available tools (name: description — input format):
  describe_feature      : PREFERRED for feature/flow questions — full Angular→Service→Repo trace for one user function — feature name (e.g. "Book Appointment", "cancel order")
  list_features         : list all detected user functions in the system — project name or "" for all
  search_deep           : PREFERRED for deep/flow/architecture questions — multi-hop re-ranked search across 8 query variants — any query string
  search_codebase       : quick semantic search over all code & digests — any query string
  search_by_project     : project-scoped semantic search — "<project>::<query>"
  get_method_calls      : method call graph for a @Service/@Repository — "ClassName" or "service-name::ClassName"
  trace_request         : trace endpoint end-to-end (Angular→Controller→Service→Repo→Entity) — "/api/path" or "GET /api/path"
  find_callers          : find everything that calls a class, endpoint, or Kafka topic — class name or "/api/path"
  impact_graph          : BFS impact analysis — class name or entity name (use for "what breaks if…" questions)
  get_all_endpoints     : list all REST endpoints + bean dependencies for a service — service name (e.g. "order-service")
  get_entity_schema     : JPA entity fields and relationships — entity class name (e.g. "Order")
  get_api_contracts     : Angular-to-backend API contract map — (empty string)
  get_service_dependencies : inter-service dependency tree — (empty string)
  get_auth_flow         : JWT auth flow summary — (empty string)
  read_source_file      : read raw source file — absolute file path
  graph_summary         : knowledge graph overview statistics — (empty string)
  get_external_calls    : list Feign downstream calls with resolved URLs for a service — service name or "" for all
  get_dto_schema        : field structure of a request/response DTO class — DTO class name (e.g. "OrderRequest")
  trace_event_flow      : full Kafka event flow — REST publisher → topic → consumers — topic name or service name\
"""

PLANNER_SYSTEM = """\
You are a planning assistant for a local code analysis AI.
Your job is to decide which tools to call to gather enough context to answer a developer's question.
Output ONLY a JSON object — no preamble, no explanation outside the JSON.\
"""

PLANNER_PROMPT = """{system}

{catalogue}

{history_block}Question: {question}
Mode: {mode}

Planning guidelines:
- "how does [feature/module/flow] work" → describe_feature (strip Module/Component suffix, e.g. BookAppointmentSlotModule → "Book Appointment Slot") + search_deep
- "end to end" / "UI to API" / "frontend to backend" / "from UI" questions → describe_feature (extract the feature/module name) + search_deep
- "what can a user do" / "what features exist" → list_features + search_deep
- deep/architecture/flow questions → search_deep + trace_request + get_method_calls (for key service classes)
- "how does X work" questions      → trace_request + search_deep + get_method_calls (on the main service)
- "who calls X" questions          → find_callers + search_deep
- "what breaks if I change X"      → impact_graph + search_deep
- "how does event X flow" / "who consumes X events" / "what events does X produce" → trace_event_flow + search_deep
- "what does [service] call" / "downstream dependencies of X" → get_external_calls + search_deep
- "what fields does X have" / "what does the request/response look like" → get_dto_schema + search_deep
- generate/implement questions     → search_codebase (patterns) + get_entity_schema (entity) + get_method_calls (similar class)
- chat/explain questions           → search_codebase + trace_request (if a path is mentioned)
- Use 3–6 tool calls; never call the same tool twice with the same input.
- For deep mode: ALWAYS use search_deep instead of search_codebase — it runs multi-hop retrieval.
- ALWAYS use describe_feature when an Angular Module, Component, or feature name is mentioned alongside "end to end", "flow", "UI", "API", or "trace".
- For describe_feature input: strip Module/Component/Service suffix from class names (BookAppointmentSlotModule → "Book Appointment Slot").

Output exactly this JSON (no markdown, no backticks):
{{"reasoning":"why these tools and inputs","tool_calls":[{{"tool":"tool_name","input":"tool_input"}}]}}
"""


# ------------------------------------------------------------------
# Planner
# ------------------------------------------------------------------

class Planner:
    def __init__(self, llm):
        self.llm = llm

    def plan(self, question: str, mode: str, history: list[dict]) -> PlanResult:
        prompt = self._build_prompt(question, mode, history)
        try:
            raw = str(self.llm.invoke(prompt))
            return self._parse(raw, question, mode)
        except Exception as exc:
            logger.warning("Planner LLM call failed (%s); using default plan", exc)
            return self._default_plan(question, mode)

    # ------------------------------------------------------------------
    # Prompt building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(question: str, mode: str, history: list[dict]) -> str:
        history_block = ""
        if history:
            tail = history[-6:]   # last 3 exchanges
            lines = ["Recent conversation:"]
            for msg in tail:
                role = "Developer" if msg["role"] == "user" else "Assistant"
                content = msg["content"][:200]
                lines.append(f"  {role}: {content}")
            history_block = "\n".join(lines) + "\n\n"
        return PLANNER_PROMPT.format(
            system=PLANNER_SYSTEM,
            catalogue=TOOL_CATALOGUE,
            history_block=history_block,
            question=question,
            mode=mode,
        )

    # ------------------------------------------------------------------
    # JSON parsing with robust fallback
    # ------------------------------------------------------------------

    def _parse(self, raw: str, question: str, mode: str) -> PlanResult:
        # Strip markdown code fences if model wraps output
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()

        # Find first {...} block
        m = re.search(r"\{[\s\S]+\}", raw)
        if not m:
            logger.warning("Planner produced no JSON; using default plan")
            return self._default_plan(question, mode)

        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as exc:
            logger.warning("Planner JSON parse error (%s); using default plan", exc)
            return self._default_plan(question, mode)

        reasoning = data.get("reasoning", "")
        raw_calls = data.get("tool_calls", [])

        from agent.tools import build_tools_map   # local import avoids circular
        valid_tools = set(build_tools_map().keys())

        tool_calls: list[ToolCall] = []
        seen: set[tuple] = set()
        for tc in raw_calls:
            name = tc.get("tool", "").strip()
            inp = str(tc.get("input", "")).strip()
            if name not in valid_tools:
                logger.debug("Planner requested unknown tool %r — skipping", name)
                continue
            key = (name, inp)
            if key in seen:
                continue
            seen.add(key)
            tool_calls.append(ToolCall(tool=name, input=inp))

        if not tool_calls:
            logger.warning("Planner produced no valid tool calls; using default plan")
            return self._default_plan(question, mode)

        return PlanResult(reasoning=reasoning, tool_calls=tool_calls)

    # ------------------------------------------------------------------
    # Rule-based fallback (delegates to module-level function)
    # ------------------------------------------------------------------

    @staticmethod
    def _default_plan(question: str, mode: str) -> PlanResult:
        return _default_plan(question, mode)


# ------------------------------------------------------------------
# Module-level rule-based default plan (no LLM needed)
# ------------------------------------------------------------------

def _default_plan(question: str, mode: str) -> PlanResult:
    q = question.lower()
    calls: list[ToolCall] = []

    _FEATURE_FLOW_WORDS = (
        "end to end", "end-to-end", "ui to api", "from ui", "from frontend",
        "frontend to backend", "how does", "how do", "flow", "trace", "walk me",
    )

    if mode == "impact" or any(w in q for w in ("break", "impact", "affect", "risk", "if i change")):
        subject = _extract_subject(question) or question[:40]
        calls.append(ToolCall("impact_graph", subject))
        calls.append(ToolCall("search_codebase", question))

    elif any(w in q for w in ("what features", "what can a user", "list features", "what modules")):
        calls.append(ToolCall("list_features", ""))
        calls.append(ToolCall("search_deep", question))

    elif any(phrase in q for phrase in _FEATURE_FLOW_WORDS):
        # Feature/module end-to-end flow question — try describe_feature first
        subject = _extract_subject(question)  # e.g. "BookAppointmentSlotModule"
        if subject:
            calls.append(ToolCall("describe_feature", subject))
        path = _extract_path(question)
        if path:
            calls.append(ToolCall("trace_request", path))
        calls.append(ToolCall("search_deep", question))
        if subject and not path:
            calls.append(ToolCall("get_method_calls", subject))

    elif mode == "deep":
        path = _extract_path(question)
        if path:
            calls.append(ToolCall("trace_request", path))
        calls.append(ToolCall("search_deep", question))
        cls = _extract_subject(question)
        if cls:
            calls.append(ToolCall("get_method_calls", cls))
        if not path:
            calls.append(ToolCall("get_api_contracts", ""))

    elif mode == "generate" or any(w in q for w in ("create", "add", "implement", "write", "generate")):
        calls.append(ToolCall("search_codebase", question))
        entity = _extract_subject(question)
        if entity:
            calls.append(ToolCall("get_entity_schema", entity))

    elif any(w in q for w in ("who calls", "what calls", "callers of", "uses")):
        subject = _extract_subject(question) or question[:40]
        calls.append(ToolCall("find_callers", subject))
        calls.append(ToolCall("search_codebase", question))

    else:
        calls.append(ToolCall("search_codebase", question))
        path = _extract_path(question)
        if path:
            calls.append(ToolCall("trace_request", path))

    return PlanResult(
        reasoning=f"[default plan for mode={mode}]",
        tool_calls=calls or [ToolCall("search_codebase", question)],
    )


# ------------------------------------------------------------------
# Tiny extraction helpers for the default planner
# ------------------------------------------------------------------

_COMMON_WORDS = {
    "What", "How", "Why", "When", "Where", "Who", "Is", "Are", "Can", "Will",
    "Does", "Do", "The", "A", "An", "If", "I", "My", "We", "You", "Please",
    "Get", "Post", "Put", "Delete", "Patch",
}


def _extract_path(text: str) -> str:
    """Find the first /api/... or /v*/... path in text."""
    m = re.search(r"(/(?:api|v\d+)/[\w/{}-]+)", text)
    return m.group(1) if m else ""


def _extract_subject(text: str) -> str:
    """Extract a likely class/entity name from text.

    Prefers compound PascalCase (OrderService) but falls back to any
    capitalised word that is not a common English word.
    """
    # Compound PascalCase first: OrderService, UserRepository, etc.
    compound = re.findall(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", text)
    if compound:
        return compound[0]
    # Single capitalised word: Order, User, Product, etc.
    single = re.findall(r"\b([A-Z][a-z]{2,})\b", text)
    for w in single:
        if w not in _COMMON_WORDS:
            return w
    return ""
