# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A fully local AI agent (no cloud) that indexes Angular + Spring Boot microservice codebases and answers developer questions about them. It uses Ollama for LLM and embeddings, ChromaDB for vector search, and a hand-built knowledge graph for structural traversal.

## Running the system

```bash
# Start the API server (port 8765)
python -m uvicorn api.server:app --host 0.0.0.0 --port 8765 --reload

# Trigger a full reindex (digest + graph + embed)
curl -X POST http://localhost:8765/reindex

# Digest only (no embedding — faster for parser development)
python -m digest.digest_runner

# Optional: file watcher (auto-reindexes on save, 2s debounce)
python -m embeddings.watcher
```

**No test suite exists.** Validate parser changes by running `python -m digest.digest_runner` and inspecting `digests/<project>.digest.json`, then checking `curl http://localhost:8765/graph/summary` after a full reindex.

## Required environment

- `projects.yaml` — registers the codebases to index (absolute paths, `angular` or `spring-boot` type)
- `.env` — copy from `.env.example`; key vars: `OLLAMA_MODEL`, `OLLAMA_EMBED_MODEL`, `CHROMA_PATH`
- Ollama daemon running with `deepseek-coder-v2` and `nomic-embed-text` pulled
- Python 3.11/3.12 with `pip install -r requirements.txt`

**Do not change `OLLAMA_EMBED_MODEL` after first index** — embedding dimensions are baked into ChromaDB and cannot be mixed.

## Architecture: the full pipeline

Every `POST /reindex` runs this sequence in `digest/digest_runner.py`:

```
projects.yaml
  → SpringBootParser / AngularParser   (per project, writes digests/*.digest.json)
  → MasterDigestBuilder                (cross-project map → digests/master.digest.json)
  → GraphBuilder + FeatureGraphBuilder (knowledge_graph.json)
  → ApiCatalogBuilder                  (api-catalog/openapi.json + api-catalog.md)
  → Chunker                            (splits digests + source + graph into chunks)
  → Embedder                           (Ollama nomic-embed-text → ChromaDB)
```

Every question follows this path in `agent/loop.py`:

```
question → Planner LLM call (picks 3–6 tools)
         → Tool execution   (graph traversal + vector search, no LLM)
         → Synthesizer LLM call (streams answer)
```

## Key design decisions to understand

**Two-LLM-call design (`agent/loop.py`):** Planner decides tools; Synthesizer writes the answer from gathered context. The Synthesizer prompt has strict grounding rules — it must answer only from gathered context, never from training knowledge. If context is thin (all tools returned < 200 chars of useful content), `_is_context_thin()` prepends a `[CONTEXT WARNING]` block.

**Knowledge graph (`graph/`):** A directed property graph stored as JSON (`graph/knowledge_graph.json`). Nodes are keyed by `type::project::name`. Edge types: `uses_service`, `http_call`, `inferred_http_call`, `handled_by`, `depends_on`, `manages`, `jpa_relation`, `feign_calls`, `produces_event`, `consumes_event`, `publishes_to`, `part_of_feature`, `feature_uses`, `feature_calls`. `GraphStore` loads this into memory and exposes BFS traversal methods called by agent tools.

**User function graph (`graph/feature_graph.py`):** Detects user-facing features from Angular component file paths (`src/app/components/<feature>/`). Creates `user_function` nodes linking Angular components → Angular services → inferred backend projects. `describe_feature()` in `graph_store.py` does fuzzy PascalCase matching (strips `Module`/`Component`/`Service` suffix, splits on word boundaries).

**ChromaDB namespacing:** Graph nodes go into `file_path="graph/nodes"`, feature chunks into `file_path="graph/features"`. These are separate to prevent hash collisions. IDs use `abs(hash(nid))` without modulo — full 64-bit to avoid collisions at 1000+ nodes.

**Java constant resolution (`digest/springboot_parser.py`):** `_build_constant_map()` scans all `.java` files for `static final String FIELD = "value"` before parsing. Used by `_extract_path_from_mapping_args()` to resolve `@GetMapping(SiteConstant.SITE_BASE_URL)` → `/api/v1/sites`.

**SpEL + property resolution for Kafka:** `@KafkaListener(topics = "#{'${spring.kafka.consumer.topic}'}")` — the `_resolve()` helper in `_parse_events()` handles SpEL wrappers `#{...}`, strips inner `'...'` string quotes, then resolves `${prop}` against `_properties` (loaded from `application.properties`).

**Feign URL resolution:** `_build_properties_map()` loads `application.properties`/`.yml`. Feign `url = "${ms-java.appointments.url}"` is resolved to the actual host value during `_parse_feign()`.

**Planner fallback (`agent/planner.py`):** If the LLM returns invalid JSON, `_default_plan()` applies keyword rules. Adding a new tool requires: (1) function + registration in `agent/tools.py`, (2) entry in `TOOL_CATALOGUE` in `agent/planner.py`, (3) planning guideline in `PLANNER_PROMPT`.

## Chunker ID scheme

Chunk IDs are `"{project}::{file_path}::{idx}"`. Digest chunks use integer offsets by type to avoid collisions within the same digest file:

| Offset | Type |
|---|---|
| 0–999 | endpoints |
| 1000–1999 | entities |
| 2000–2999 | feign clients |
| 3000–3499 | beans |
| 3500–3999 | method call graphs |
| 4000–4499 | exception handlers |
| 4500–4799 | scheduled tasks |
| 4800–4899 | migrations |
| 5000–5999 | Angular components |
| 6000–6999 | Angular services |
| 7000–7999 | NgRx features |
| 8000+ | DTO schemas |

## Generated artifacts (all safe to delete and regenerate)

- `digests/` — per-project and master JSON digests
- `graph/knowledge_graph.json` — the graph
- `vector_db/` — ChromaDB collections
- `api-catalog/openapi.json` + `api-catalog/api-catalog.md` — generated API catalog

## Adding a new agent tool

1. Write the function in `agent/tools.py`, register in `build_tools_map()` and `build_tools()`
2. Add one line to `TOOL_CATALOGUE` in `agent/planner.py`
3. Add a planning guideline in `PLANNER_PROMPT` for which question patterns should trigger it
4. If it needs graph data, add a method to `graph/graph_store.py`
