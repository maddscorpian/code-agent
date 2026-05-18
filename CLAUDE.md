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

**Split LLM instances (`agent/agent_core.py`):** Two separate `Ollama()` instances are created — `planner_llm` (`num_ctx=4096, temperature=0.1`) and `synth_llm` (`num_ctx=8192, temperature=0.15, top_p=0.95`). The planner prompt is only ~1K–2K tokens; giving it 4K context keeps it fast. The synthesizer handles gathered tool output (typically 3K–7K tokens) and needs 8K. Using 16384 for both caused the KV cache to be 4× too large, forcing CPU offloading and 20–60 s wait times. `AgentLoop.__init__` accepts `planner_llm=None` (falls back to `llm` if not provided). `RAGChain` (fallback path) uses `num_ctx=8192`.

**Progress events (`agent/loop.py`, `api/server.py`, `api/static/chat.html`):** `stream_run()` yields `_PROGRESS_SENTINEL` / `_PROGRESS_SENTINEL_END` markers before the planner call, before each tool, and before synthesis. `server.py` parses these into `event: progress` SSE events. The chat UI listens for `event: progress` and updates the status line: "thinking — planning tool calls…" → "running: search_deep (1 of 3)…" → "synthesizing answer…".

**Context assembly (`agent/loop.py`):** `_MAX_TOOL_OUTPUT = 12000`. Also `_PROGRESS_SENTINEL` / `_PROGRESS_SENTINEL_END` for phase tracking. (raised from 6000). Tool results are sorted so `_STRUCTURAL_TOOLS` (graph traversal: `trace_request`, `describe_feature`, `get_method_calls`, `impact_graph`, `find_callers`, `trace_event_flow`) appear first — they carry verified class chains and anchor the answer before softer semantic search evidence. Results are deduplicated across tool calls by fingerprinting paragraph segments (first 200 chars); duplicate content from two tools is dropped to reclaim context budget. Each section is labelled `[STRUCTURAL]` or `[SEMANTIC]` and the system prompt tells the LLM to prefer STRUCTURAL when sources conflict.

**Query variants (`agent/tools.py`, `agent/rag_chain.py`):** `search_deep` and the RAG chain each run 10 targeted query variants (raised from 7/8) covering: service/business logic, REST endpoints, entities/repos, config/Feign/Kafka, Angular frontend, method call graphs, **security/auth (@PreAuthorize, JWT, roles)**, **exception handling (@ControllerAdvice)**, **Kafka event/config (topic, property)**. Chat-mode `search_codebase` retrieves 16 candidates and reranks to best 8 using `_rerank_hits` (was raw top-k=8 with no reranking).

**Graph traversal enrichment (`graph/graph_store.py`):** `_get_digest_queries(project, class_name)` reads `@Query` JPQL/SQL strings from digest files on demand (graph nodes don't carry them — only the digest JSON does). Called by `_format_forward_chain()` for `spring_repository` nodes (used by `trace_request`) and by `describe_feature()` for dependency repositories. `trace_request()` also appends `[AUTH: required, roles=[...]]` or `[AUTH: public]` to every endpoint label.

**Knowledge graph (`graph/`):** A directed property graph stored as JSON (`graph/knowledge_graph.json`). Nodes are keyed by `type::project::name`. Edge types: `uses_service`, `http_call`, `inferred_http_call`, `handled_by`, `depends_on`, `manages`, `jpa_relation`, `feign_calls`, `produces_event`, `consumes_event`, `publishes_to`, `part_of_feature`, `feature_uses`, `feature_calls`. `GraphStore` loads this into memory and exposes BFS traversal methods called by agent tools.

**User function graph (`graph/feature_graph.py`):** Detects user-facing features from Angular component file paths (`src/app/components/<feature>/`). Creates `user_function` nodes linking Angular components → Angular services → inferred backend projects. `describe_feature()` in `graph_store.py` does fuzzy PascalCase matching (strips `Module`/`Component`/`Service` suffix, splits on word boundaries).

**ChromaDB namespacing:** Graph nodes go into `file_path="graph/nodes"`, feature chunks into `file_path="graph/features"`. These are separate to prevent hash collisions. IDs use `abs(hash(nid))` without modulo — full 64-bit to avoid collisions at 1000+ nodes.

**Java constant resolution (`digest/springboot_parser.py`):** `_build_constant_map()` scans all `.java` files for `static final String FIELD = "value"` before parsing. Used by `_extract_path_from_mapping_args()` to resolve `@GetMapping(SiteConstant.SITE_BASE_URL)` → `/api/v1/sites`.

**NoSQL document entity detection (`digest/springboot_parser.py`):** `_NOSQL_DOCUMENT_ANNOTATIONS = {"Document", "RedisHash", "Node", "DynamoDBTable"}` — the AST parser flags `has_entity=True` for any of these, just like `@Entity`. `_parse_entities()` branches on `has_jpa` vs `has_nosql`: for MongoDB it calls `_extract_document_collection()` to get the collection name. `@Field` is treated the same as `@Column` for field extraction; `@DBRef` is treated like JPA `@OneToMany`. `_REPO_SUPERTYPES` covers `MongoRepository`, `ReactiveMongoRepository`, and all other Spring Data supertypes — interfaces that extend these are detected as repositories even without `@Repository` annotation.

**MongoDB collection name resolution (`_extract_document_collection`):** Uses paren-depth scanning (not `[^)]*` regex) to capture the full `@Document(...)` argument including nested parens from SpEL. Joins Java string concatenation (`"part1"\n + "part2"` → `"part1part2"`) before parsing. Extracts the full double-quoted Java string (single quotes are allowed inside for SpEL keys). SpEL `#{@environment.getProperty('key') ?: ''}` is resolved: `key` is looked up in `self._properties` (from `application.properties`); the resolved suffix is appended to the base name. If the property is absent, the base name is returned unchanged (matches the `?: ''` default).

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

## Documentation rule

**Always update `README.md` and `CLAUDE.md` as part of every code change.** This is not optional.

- `README.md` — update any section that describes behaviour that changed: "What It Understands", system component descriptions, tool tables, API reference, sample prompts, troubleshooting. If a new capability was added, add an entry. If behaviour changed, update the description.
- `CLAUDE.md` — update the "Key design decisions" section whenever an architectural decision changes (e.g. new constants, changed defaults, new resolution strategies). Update the "Adding a new agent tool" steps if the process changes.

The documentation update must be in the **same commit** as the code change — not a follow-up commit.
