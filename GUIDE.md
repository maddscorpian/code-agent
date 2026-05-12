# Local AI Agent — Complete Guide

A fully local, Ollama-powered code assistant built for Angular + Spring Boot microservice projects.
No cloud required. All LLM calls, embeddings, and vector storage run on your machine.

---

## Table of Contents

1. [What This System Does](#1-what-this-system-does)
2. [System Architecture](#2-system-architecture)
3. [Key Modules](#3-key-modules)
4. [Setting Up on a New System](#4-setting-up-on-a-new-system)
5. [Configuration](#5-configuration)
6. [Running the System](#6-running-the-system)
7. [First-Time Indexing](#7-first-time-indexing)
8. [Using the Chat UI](#8-using-the-chat-ui)
9. [VS Code Extension](#9-vs-code-extension)
10. [API Reference](#10-api-reference)
11. [How Each Phase Works](#11-how-each-phase-works)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. What This System Does

This agent understands your entire codebase — every Spring Boot service, every Angular component, every Feign client, Kafka topic, JPA entity, and DB migration — and can answer questions, trace flows, analyze impact, and generate code that matches your exact patterns.

### Core Capabilities

| Capability | Description |
|---|---|
| **Code Q&A** | Ask anything about your codebase. Gets concrete answers with class names, file paths, method names. |
| **End-to-end trace** | "How does the order creation flow work?" → traces Angular component → service → HTTP call → controller → service bean → repository → entity → DB table |
| **Impact analysis** | "What breaks if I rename the Order entity?" → BFS through the knowledge graph across all services |
| **Code generation** | "Add a cancel endpoint to OrderController" → generates proper unified diffs matching your code style, with an Apply button |
| **Multi-turn conversation** | Remembers context across the session: "Now add validation to that" works as expected |
| **Diff apply** | Generated code diffs can be applied to disk directly from the chat UI |

### What It Understands

**Spring Boot:**
- REST controllers (endpoints, methods, request/response DTOs, auth/roles)
- Service beans (`@Service`) with constructor injection dependencies
- Repository beans (`@Repository`) and query method names
- JPA entities (fields, relationships, table names)
- Feign clients (which service they call, which endpoints)
- Exception handlers (`@ControllerAdvice`)
- Scheduled tasks (`@Scheduled` with cron/fixedRate)
- Kafka/RabbitMQ producers and consumers
- Security configuration (JWT filters, permit-all paths, OAuth2)
- Build dependencies (`pom.xml` / `build.gradle`)
- DB migration history (Flyway `.sql` / Liquibase `changelog.xml`)
- `application.yml` per-profile config

**Angular:**
- Modules, components (inputs, outputs, injected services, template events)
- Services with HTTP calls (method, URL, response type)
- Routes (eager and lazy-loaded), guards, interceptors (JWT detection)
- NgRx: actions, effects, selectors
- Models, interfaces, enums
- Environment config (API base URLs)

**Cross-cutting:**
- Full Angular → backend API contract map
- Inter-service dependency graph (Feign + Kafka edges)
- JWT auth flow (issuer service, validators, FE interceptor)
- Shared DTOs across services

---

## 2. System Architecture

```
Developer
    │
    ├── Browser chat UI  (api/static/chat.html)
    └── VS Code extension  (vscode-extension/)
              │
              ▼
    FastAPI backend  (api/server.py :8765)
              │
    ┌─────────┴──────────────────────────┐
    │                                    │
    AgentCore                        GraphStore
    (agent/agent_core.py)            (graph/graph_store.py)
    │                                    │
    ├── Planner (1 LLM call)         loaded from
    │   (agent/planner.py)           graph/knowledge_graph.json
    │                                    │
    ├── Tool execution                   │
    │   (agent/tools.py) ───────────────►│
    │   ├── search_codebase              │
    │   ├── trace_request ───────────────┘
    │   ├── find_callers
    │   ├── impact_graph
    │   ├── get_entity_schema
    │   └── read_source_file ...
    │
    └── Synthesizer (1 LLM call, streaming)
        (agent/loop.py)
              │
    ┌─────────┴───────────────────────┐
    │                                 │
Ollama LLM                    ChromaDB vector store
(deepseek-coder-v2)           (embeddings/vector_store.py)
(nomic-embed-text)            ./vector_db/

Knowledge graph ◄── built from ── Digest files
(graph/)                          (digests/*.digest.json)
                                        ▲
                              Digest engine
                              (digest/)
                                  ├── SpringBootParser  (javalang AST + regex)
                                  ├── AngularParser     (regex + NgRx detection)
                                  ├── PomParser         (pom.xml / build.gradle)
                                  └── MasterDigestBuilder
```

### Request Flow (every question)

```
User question
  → Session lookup (agent/session_store.py) — load conversation history
  → Planner LLM call — decides 2–6 tools to call
  → Tool execution (all local, no LLM) — graph traversal, vector search, file reads
  → SSE event: plan — sent to client (shows "Gathered via: [tool1] [tool2]")
  → Synthesizer LLM call — streams answer tokens using all gathered context
  → Session save — question + answer stored for next turn
  → Client renders markdown + applies diff toolbars
```

---

## 3. Key Modules

### `digest/` — Code Understanding Engine

| File | Purpose |
|---|---|
| `springboot_parser.py` | Parses Java files using `javalang` AST (with regex fallback). Extracts endpoints, service/repository beans, entities, Feign clients, events, exception handlers, scheduled tasks. |
| `angular_parser.py` | Parses TypeScript files. Extracts components, services (with HTTP calls + URL resolution), routes, NgRx features, environments, interceptors. |
| `pom_parser.py` | Parses `pom.xml` / `build.gradle` for dependencies. Scans Flyway `.sql` and Liquibase `changelog.xml` for migration summaries. |
| `master_digest_builder.py` | Cross-service map: API contracts (Angular → backend), service dependency graph (Feign + Kafka), shared DTOs, auth flow. |
| `digest_runner.py` | Orchestrates full/single/incremental digest runs. Also triggers graph rebuild after every run. |
| `models.py` | Pydantic schemas for all digest objects (v2.0): `ServiceDigest`, `AngularDigest`, `BeanDigest`, `NgRxFeature`, `ScheduledTaskDigest`, etc. |

### `graph/` — Knowledge Graph

| File | Purpose |
|---|---|
| `graph_builder.py` | Builds a directed property graph from all digest files. Node types: `endpoint`, `bean` (spring_service/repository/component), `entity`, `angular_component`, `angular_service`, `kafka_topic`. Edge types: `uses_service`, `http_call`, `handled_by`, `depends_on`, `manages`, `jpa_relation`, `feign_calls`, `produces_event`, `consumes_event`. |
| `graph_store.py` | Loads graph from `graph/knowledge_graph.json` into memory. Provides BFS traversal tools: `trace_request`, `find_callers`, `impact_graph`, `summary`. All return human-readable strings for the LLM. |

### `embeddings/` — Vector Index

| File | Purpose |
|---|---|
| `chunker.py` | Converts digest JSON + source files + graph relationships into chunks. Uses semantic splitting (method-level for Java/TypeScript). Generates chunks for beans, migrations, NgRx features, graph relationships. |
| `embedder.py` | Calls Ollama embedding model (`nomic-embed-text`) to vectorise each chunk. |
| `vector_store.py` | ChromaDB wrapper. Upsert, query (with metadata filters), delete by project. |
| `watcher.py` | `watchdog` file-system watcher. Debounces (2s) then re-digests only the affected project. |

### `agent/` — Reasoning Engine

| File | Purpose |
|---|---|
| `planner.py` | LLM call #1: decides which tools to invoke. Parses JSON output robustly (strips markdown fences, extracts `{...}`, falls back to rule-based `_default_plan`). |
| `loop.py` | Orchestrates Plan → Execute → Synthesize. `stream_run()` emits a `__PLAN__` sentinel before tokens so the server can send `event: plan` SSE. Mode-specific synthesis prompts (chat / deep / generate / impact). |
| `agent_core.py` | Public interface: `run()` and `stream_run()`. Initialises `AgentLoop` with graceful fallback to `RAGChain` single-shot if loop setup fails. |
| `rag_chain.py` | Fallback single-shot RAG: embed query → retrieve top chunks → prompt → LLM. Used when the full loop fails or is unavailable. |
| `tools.py` | All tool functions (plain Python callables). `build_tools_map()` returns `{name: fn}` for the loop. Includes vector search, digest lookups, and the three graph tools. |
| `session_store.py` | Thread-safe in-memory session store. 2-hour TTL, max 200 sessions. Each session holds conversation history (last 6 turns injected into prompts). |
| `prompts.py` | System prompt, per-mode prompt templates, `format_history()`. |
| `code_gen.py` | Parses LLM output for `### FILE: [MODIFY|CREATE]` blocks. Applies unified diffs using system `patch` (fallback: pure Python hunk applier). Path validation against registered project roots. |

### `api/` — HTTP Layer

| File | Purpose |
|---|---|
| `server.py` | FastAPI app. All endpoints (see API Reference). Manages session store and graph store lifecycle. |
| `schemas.py` | Pydantic request/response models including `ApplyRequest`, `ApplyResponse`. |
| `middleware.py` | Request logging and latency. |
| `static/chat.html` | Single-file browser chat UI. Markdown rendering (marked.js), syntax highlighting (highlight.js), diff renderer with Apply/Copy buttons, session persistence, plan strip. |

---

## 4. Setting Up on a New System

> These steps assume you have already cloned the repository. Follow them in order — each step depends on the previous one.

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.12 also works |
| Ollama | latest | [ollama.com](https://ollama.com) |
| `patch` | system | Pre-installed on macOS/Linux; required by the `/apply` endpoint |
| Node.js | 18+ | Only needed if building the VS Code extension |

---

### Step 1 — Install Ollama

**macOS:**
```bash
brew install ollama
```

**Linux:**
```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Start the Ollama daemon — keep this running in its own terminal for the entire session:
```bash
ollama serve
```

---

### Step 2 — Pull the Required Models

```bash
# Main LLM — used for planning, Q&A, and code generation (~9 GB)
ollama pull deepseek-coder-v2

# Embedding model — used to vectorise code chunks (~274 MB)
ollama pull nomic-embed-text

# Confirm both downloaded successfully
ollama list
```

> **Low VRAM / slow machine?** Use a lighter model instead:
> ```bash
> ollama pull qwen2.5-coder:7b    # ~4 GB, good quality
> # or
> ollama pull codellama:13b        # ~8 GB
> ```
> Then set `OLLAMA_MODEL=qwen2.5-coder:7b` in your `.env` (Step 4).

---

### Step 3 — Create the Python Virtual Environment

```bash
cd local-ai-agent        # the cloned repo root

python3 -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

All subsequent commands assume the virtual environment is active.

---

### Step 4 — Create the `.env` File

```bash
cp .env.example .env
```

Open `.env` and review each value. The defaults work for a standard local setup:

```env
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=deepseek-coder-v2
OLLAMA_EMBED_MODEL=nomic-embed-text
CHROMA_PATH=./vector_db
DIGESTS_PATH=./digests
PROJECTS_CONFIG=./projects.yaml
API_PORT=8765
LOG_LEVEL=INFO
```

**Only change these if necessary:**

| Variable | When to change |
|---|---|
| `OLLAMA_HOST` | Ollama is running on a different machine — set to `http://<ip>:11434` |
| `OLLAMA_MODEL` | You pulled a lighter model in Step 2 — set to its name |
| `API_PORT` | Port 8765 is already in use on this machine |

> **Do not change `OLLAMA_EMBED_MODEL` after running your first index.** Vectors are tied to the embedding model — switching models requires deleting `vector_db/` and re-indexing from scratch.

---

### Step 5 — Register Your Projects in `projects.yaml`

Open `projects.yaml` and replace the placeholder paths with the **absolute paths** to your actual codebases on this machine:

```yaml
workspace: /Users/yourname/projects    # informational only, not used by code

projects:
  frontend:
    - name: fe-app                     # short unique identifier (no spaces)
      type: angular
      path: /Users/yourname/projects/fe-app   # ← absolute path, must exist

  services:
    - name: user-service
      type: spring-boot
      path: /Users/yourname/projects/user-service

    - name: order-service
      type: spring-boot
      path: /Users/yourname/projects/order-service

    - name: api-gateway
      type: spring-boot
      path: /Users/yourname/projects/api-gateway
      port: 8080                       # optional, informational only
```

**Rules:**
- `name` — used internally in tool calls and digest filenames; keep it short with no spaces
- `type` — `angular` for Angular; `spring-boot` (also accepts `spring`, `maven`, `gradle`) for Spring Boot
- `path` — must be an **absolute** path that **exists on disk**; relative paths will not work
- Add or remove service entries to match exactly what you have

---

### Step 6 — Start the API Server

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8765 --reload
```

Expected output:
```
INFO:     Uvicorn running on http://0.0.0.0:8765
INFO:     Application startup complete.
```

Leave this running. Open a new terminal for the next steps.

---

### Step 7 — Build Digests, Graph, and Embeddings

This is the indexing step. It parses your code, builds the knowledge graph, embeds everything, and stores it in ChromaDB. **Must be done before the agent can answer any questions.**

```bash
curl -X POST http://localhost:8765/reindex
```

This single command runs the full pipeline:
1. Parses every registered project (Spring Boot + Angular) into structured JSON digests
2. Builds the cross-service knowledge graph
3. Chunks all digest data + source code + graph relationships
4. Embeds each chunk using `nomic-embed-text`
5. Stores vectors in ChromaDB

Wait for the response — it can take **2–15 minutes** depending on codebase size:

```json
{
  "status": "ok",
  "projects_indexed": ["fe-app", "user-service", "order-service"],
  "chunks_created": 3842,
  "duration_ms": 47231
}
```

> If the command times out, the server is still running the index in the background. Check `/health` after a minute.

---

### Step 8 — Verify Everything is Working

Run these checks in order:

```bash
# 1. Ollama and ChromaDB must both show true
curl http://localhost:8765/health
```
```json
{"status":"ok","ollama":true,"chromadb":true,"model":"deepseek-coder-v2"}
```

```bash
# 2. All registered projects should show exists=true
curl http://localhost:8765/projects
```
```json
[
  {"name":"fe-app","type":"angular","path":"/Users/...","exists":true},
  {"name":"user-service","type":"spring-boot","path":"/Users/...","exists":true}
]
```

```bash
# 3. Digest summary — confirms parsing ran successfully
curl http://localhost:8765/digest
```
```json
{"projects":["fe-app","user-service","order-service"],"total_endpoints":42,"total_entities":15,...}
```

```bash
# 4. Knowledge graph — confirms graph was built
curl http://localhost:8765/graph/summary
```
```json
{"summary":"Knowledge Graph: 87 nodes, 134 edges\n...","stats":{"nodes":87,"edges":134}}
```

If any check fails, see [Section 12 — Troubleshooting](#12-troubleshooting).

---

### Step 9 — Test the Chat

Open the browser UI at `http://localhost:8765/chat` and run these test questions to confirm each part of the system is working:

| What to ask | What it tests |
|---|---|
| `What services are in this system?` | Basic RAG — vector search + LLM responding |
| `How does GET /api/orders work?` | Knowledge graph — trace from Angular to DB |
| `What breaks if I change the User entity?` | Graph BFS — impact_graph tool |
| `Add a health check endpoint to user-service` | Generate mode — diff output with Apply button |
| Ask anything, then follow up with `Now explain the service layer for that` | Session memory — multi-turn conversation |

All five should return relevant, codebase-specific answers. If they return generic responses, the index is empty — re-run Step 7.

---

### Quick Troubleshooting

| Problem | Fix |
|---|---|
| `"ollama": false` in health check | Run `ollama serve` and confirm `ollama list` shows both models |
| `"chromadb": false` in health check | Run `curl -X POST http://localhost:8765/reindex` |
| `"exists": false` for a project | The path in `projects.yaml` is wrong for this machine — fix the absolute path |
| Weak or irrelevant answers | Index is stale — re-run `curl -X POST http://localhost:8765/reindex` |
| `graph not built` message in chat | Re-run reindex; graph builds as part of the pipeline |
| Port 8765 in use | Start with `--port 9000` and open `http://localhost:9000/chat` |
| Model too slow / out of memory | Pull `qwen2.5-coder:7b`, set `OLLAMA_MODEL=qwen2.5-coder:7b` in `.env`, restart server |

---

## 5. Configuration

### `.env` Reference

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API URL. Change if running Ollama on a different machine or port. |
| `OLLAMA_MODEL` | `deepseek-coder-v2` | LLM for code generation and Q&A. Must be pulled via `ollama pull`. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model. Must be pulled. Do not change after first index (vectors will mismatch). |
| `CHROMA_PATH` | `./vector_db` | Directory where ChromaDB stores vectors. Created automatically. |
| `DIGESTS_PATH` | `./digests` | Directory where digest JSON files are written. Created automatically. |
| `PROJECTS_CONFIG` | `./projects.yaml` | Path to your projects registry file. |
| `API_PORT` | `8765` | Port the FastAPI server listens on. |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`). |

### `projects.yaml` Reference

```yaml
workspace: /path/to/workspace   # informational only, not used by code

projects:
  frontend:
    - name: <project-name>       # unique identifier, used in tool calls
      type: angular
      path: /absolute/path       # must exist on disk

  services:
    - name: <service-name>
      type: spring-boot
      path: /absolute/path
      port: 8080                 # optional, informational
```

---

## 6. Running the System

### Start the API Server

```bash
# Activate virtual environment first
source .venv/bin/activate

# From the project root
uvicorn api.server:app --host 0.0.0.0 --port 8765 --reload
```

The `--reload` flag auto-restarts on code changes. Remove it in production.

### Optional: Start the File Watcher

The watcher re-indexes a project automatically when you save a file:

```bash
# In a separate terminal (with venv active)
python -m embeddings.watcher
```

The watcher debounces 2 seconds after the last file change, then re-digests and re-embeds only the affected project.

---

## 7. First-Time Indexing

Indexing must be done before the agent can answer questions. It has two sub-steps:

**Step 1 — Digest (parse your code into structured JSON):**
```bash
python -m digest.digest_runner
```

This writes:
- `digests/<project-name>.digest.json` — per-service/frontend structured map
- `digests/master.digest.json` — cross-service contracts and dependency graph
- `graph/knowledge_graph.json` — directed property graph of all relationships

**Step 2 — Embed (vectorise and store in ChromaDB):**

Call the reindex API endpoint (after starting the server):

```bash
curl -X POST http://localhost:8765/reindex
```

Or click the **Reindex** button in the browser chat UI.

This runs digest + embedding in one shot and reports how many chunks were indexed.

### Verify the Index

```bash
# Check health
curl http://localhost:8765/health

# Check digest summary
curl http://localhost:8765/digest

# Check graph statistics
curl http://localhost:8765/graph/summary
```

Expected health response:
```json
{"status":"ok","ollama":true,"chromadb":true,"model":"deepseek-coder-v2"}
```

---

## 8. Using the Chat UI

Open `http://localhost:8765/chat` in your browser.

### Interface

```
┌─────────────────────────────────────────────────────────┐
│ Mode: [chat▼]  [Health]  [New Session]  session: abc123… │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  You: how does the order creation flow work?            │
│                                                         │
│  ┌─────────────────────────────────────────────────┐   │
│  │ Gathered via: [trace_request] [search_codebase] │   │
│  │                                                 │   │
│  │ The order creation flow starts in Angular's     │   │
│  │ OrderService.createOrder() which calls...       │   │
│  └─────────────────────────────────────────────────┘   │
│                                                         │
├─────────────────────────────────────────────────────────┤
│ [textarea: Ask about your codebase...]                  │
│ [Send]  [Reindex]  Streaming uses /ask/stream           │
└─────────────────────────────────────────────────────────┘
```

### Modes

| Mode | Best for | Example |
|---|---|---|
| **chat** | General questions, explanations | "What does UserService do?" |
| **deep** | Architecture traces, root cause | "Walk me through the auth flow end to end" |
| **generate** | Writing new code | "Add a cancel endpoint to OrderController" |
| **impact** | Risk analysis before changes | "What breaks if I change the Order entity?" |

Mode is auto-detected from your question if left on `chat`. Switch manually for best results.

### Generated Code — Apply Button

When the agent generates code in **generate** mode, every code block gets a toolbar:

- **Diff blocks** (`--- a/` / `+++ b/` format): show the target file path + an **Apply** button
- **All blocks**: **Copy** button

Clicking **Apply** sends the diff to `POST /apply`. The server validates the path is inside a registered project before writing anything. The button shows `✓ Applied` on success or `✗ Failed` with the error in the status bar.

### Session Management

- Sessions persist across page reloads (stored in `localStorage`)
- The last 4 conversation exchanges are injected into every LLM prompt
- Click **New Session** to start a fresh conversation (clears history)
- Sessions expire after 2 hours of inactivity

### Keyboard Shortcut

`Ctrl+Enter` (or `Cmd+Enter` on Mac) sends the message without clicking Send.

---

## 9. VS Code Extension

The extension provides in-editor chat, CodeLens, and right-click commands.

### Build and Install

```bash
cd vscode-extension
npm install
npm run compile

# Install into VS Code
code --install-extension local-ai-agent-0.1.0.vsix
# Or: open VS Code → Extensions → "..." → Install from VSIX
```

### Available Commands

Access via `Ctrl+Shift+P` (Command Palette):

| Command | Description |
|---|---|
| `Local AI: Open Chat` | Opens the chat panel |
| `Local AI: Explain This` | Explains the selected code (context menu) |
| `Local AI: Generate Change` | Prompts for a change request on the current file |
| `Local AI: Impact Analysis` | Impact analysis on the selected code |
| `Local AI: Re-index Codebase` | Triggers a full reindex |

### CodeLens

The extension adds clickable actions above:
- `@RestController`, `@GetMapping`, `@PostMapping` etc. → "Explain this endpoint"
- `@Entity` → "Show entity relationships"
- `@Component`, `@Injectable` (Angular) → "Explain this component/service"

### Extension Settings

The extension connects to `http://localhost:8765` by default. The API server must be running before opening the chat panel.

---

## 10. API Reference

Base URL: `http://localhost:8765`

### Chat

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/ask` | Single-shot question → answer (JSON) |
| `POST` | `/ask/stream` | Streaming question → SSE token stream |
| `DELETE` | `/session/{id}` | Clear a conversation session |

**`POST /ask` body:**
```json
{
  "question": "How does order creation work?",
  "mode": "deep",
  "session_id": "abc-123",
  "file_context": "optional current file content"
}
```

**`POST /ask/stream` SSE events:**
```
event: session
data: <session-uuid>

event: plan
data: {"reasoning":"...","tools":["trace_request","search_codebase"]}

data: <token>
data: <token>
...

event: done
data: <session-uuid>
```

### Code Apply

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/apply` | Apply a unified diff to registered project files |
| `POST` | `/apply/file` | Create or overwrite a file with full content |

**`POST /apply` body:**
```json
{
  "diff": "--- a/src/...\n+++ b/src/...\n@@ ...",
  "project": "order-service"
}
```

### Indexing

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/reindex` | Runs digest + embed + vector upsert for all or one project |

**`POST /reindex` body:**
```json
{ "project": "order-service" }    // optional — omit to reindex all
```

### Knowledge Graph

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/graph` | Full graph JSON |
| `GET` | `/graph/summary` | Node/edge count statistics |
| `GET` | `/graph/trace?q=/api/orders` | End-to-end request trace |
| `GET` | `/graph/callers?q=OrderService` | Find callers of a class/endpoint |
| `GET` | `/graph/impact?q=Order` | BFS impact analysis |

### System

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Ollama + ChromaDB status |
| `GET` | `/digest` | Digest summary (project count, endpoints, entities) |
| `GET` | `/projects` | Registered projects from `projects.yaml` |
| `GET` | `/chat` | Browser chat UI |

---

## 11. How Each Phase Works

### Phase 1 — Deep Parsing

**What changed:** Replaced regex-only parsers with `javalang` AST + regex hybrid for Spring Boot, and improved TypeScript parsing for Angular.

**Spring Boot now extracts:**
- Service/repository/component beans with constructor injection dependencies
- `@Transactional` methods per bean
- `@Scheduled` tasks with cron/fixedRate expressions
- `@ControllerAdvice` exception handlers
- `pom.xml` / `build.gradle` dependencies
- Flyway SQL / Liquibase XML migration summaries
- Per-profile `application-*.yml` config
- Kafka `@KafkaListener` topics and `kafkaTemplate.send()` topics

**Angular now extracts:**
- NgRx: `createAction`, `createEffect`, `createSelector` from `*.actions/effects/selectors.ts`
- Environment files → API base URLs
- Template event bindings (`(click)`, `(submit)`) from matching `.html` files
- HTTP call URL resolution (dereferences `this.apiUrl + '/path'` to the literal)

**Chunking improvements:**
- Java files split at method boundaries (not token count)
- TypeScript files split at function/class boundaries
- New chunk types: bean, scheduled task, exception handler, migration, NgRx feature, graph relationship

### Phase 2 — Knowledge Graph

**What changed:** `graph/knowledge_graph.json` is built and maintained automatically.

**Graph structure:**
```
Angular component
  --[uses_service]--> Angular service
    --[http_call]--> endpoint (order-service GET /api/orders)
      --[handled_by]--> OrderService (spring_service bean)
        --[depends_on]--> OrderRepository (spring_repository bean)
          --[manages]--> Order (entity, table=orders)
            --[jpa_many_to_one]--> User (entity, user-service)

order-service OrderService
  --[feign_calls]--> user-service GET /api/users/{id}
  --[produces_event]--> kafka_topic: order.created

kafka_topic: order.created
  --[consumes_event]--> payment-service PaymentService
```

Three traversal tools available in every agent call:
- `trace_request("/api/orders")` — full Angular→DB chain
- `find_callers("OrderService")` — all inbound edges
- `impact_graph("Order")` — bidirectional BFS with risk level

The graph is rebuilt automatically after every `/reindex` call.

### Phase 3 — Conversation Memory

**What changed:** Every question is now in the context of the ongoing conversation.

- `session_id` returned in every `/ask` and `/ask/stream` response
- Stored in browser `localStorage` — survives page reload
- Last 4 exchanges (8 messages) injected into every LLM prompt
- Thread-safe `SessionStore`: 2-hour TTL, max 200 concurrent sessions
- **New Session** button resets history

### Phase 4 — Agentic Loop (Plan → Gather → Synthesize)

**What changed:** Replaced single-shot RAG and brittle ReAct with a clean two-LLM-call design.

**Call 1 — Planner:**
- Sees: question + mode + tool catalogue + recent history
- Outputs JSON: `{"reasoning": "...", "tool_calls": [...]}`
- Decides 2–6 tools to call
- Falls back to rule-based default plan if JSON parsing fails

**Tool execution (no LLM):**
- All tools run locally: graph traversal, vector search, file reads, digest lookups
- Output capped at 3,000 chars per tool to protect context window

**Call 2 — Synthesizer (streams):**
- Sees: all tool results + mode-specific instructions + history
- Streams tokens back to client

**The planner's decision rules:**
- "how does X work" → `trace_request` + `search_codebase`
- "what breaks if I change X" → `impact_graph` + `search_codebase`
- "who calls X" → `find_callers` + `search_codebase`
- "generate / add / implement" → `search_codebase` + `get_entity_schema`
- "deep / explain / walk me through" → `trace_request` + `search_codebase` + `get_api_contracts`

### Phase 5 — Code Generation with Diffs

**What changed:** Generate mode now outputs machine-parseable structured diffs, not free-form text.

**Output format the LLM is instructed to produce:**
```
### FILE: src/main/java/.../OrderController.java [MODIFY]
```diff
--- a/src/main/java/.../OrderController.java
+++ b/src/main/java/.../OrderController.java
@@ -30,6 +30,11 @@
     ...3 context lines...
+    @DeleteMapping("/{id}/cancel")
+    public ResponseEntity<Void> cancel(@PathVariable Long id) {
+        orderService.cancel(id);
+        return ResponseEntity.noContent().build();
+    }
     }
```

### FILE: src/test/java/.../OrderControllerTest.java [CREATE]
```java
// complete test class
```
```

**Apply pipeline:**
1. Chat UI renders diff with color coding (highlight.js diff language)
2. Apply button extracts raw diff text
3. `POST /apply` validates path is inside a registered project
4. Applies using system `patch --forward -u` (fallback: pure Python hunk applier)
5. Returns `{status, files_modified, files_created}`

---

## 12. Troubleshooting

### Ollama not reachable

```
health=degraded ollama=false
```

**Fix:**
```bash
ollama serve            # start if not running
ollama list             # verify models are pulled
```

### Models not found

```
Error: model 'deepseek-coder-v2' not found
```

**Fix:**
```bash
ollama pull deepseek-coder-v2
ollama pull nomic-embed-text
```

### Empty or weak answers

The vector index is empty or stale.

**Fix:** Run reindex:
```bash
curl -X POST http://localhost:8765/reindex
```
Or click **Reindex** in the chat UI.

### Graph not built

```
Knowledge graph not built. Run /reindex to build it.
```

**Fix:** Reindex first. The graph is built as part of the reindex pipeline.

### `apply` returns error: path not inside project

The LLM generated a path that doesn't match your project structure.

**Fix:** Check that the path in the generated diff matches the actual structure in `projects.yaml`. Common cause: the project name in the diff prefix doesn't match the registered `name`.

You can also apply manually:
```bash
# From your project root:
patch -p1 < changes.patch
```

### javalang parse failures

Some Java 17+ syntax (records, sealed classes, text blocks, switch expressions) may cause `javalang` to fail. The parser automatically falls back to regex in this case — no action needed, but coverage may be reduced for those files.

### Changing the embedding model

If you change `OLLAMA_EMBED_MODEL` after the first index, you **must** delete `vector_db/` and reindex from scratch. Embedding dimensions differ between models and cannot be mixed.

```bash
rm -rf vector_db/
curl -X POST http://localhost:8765/reindex
```

### High memory usage

`deepseek-coder-v2` requires ~16GB VRAM. For lower-spec machines:

```env
OLLAMA_MODEL=codellama:13b        # ~8GB VRAM
# or
OLLAMA_MODEL=qwen2.5-coder:7b    # ~4GB VRAM
```

Answer quality will be lower but the system will still work.

### Port conflict

If port 8765 is in use:

```bash
# Use a different port
uvicorn api.server:app --port 9000

# Update API_PORT in .env if using the VS Code extension
```

---

## Generated Artifacts

The system creates these directories at runtime — all safe to delete and regenerate:

| Path | Contents | Regenerate with |
|---|---|---|
| `digests/` | Structured JSON code maps per project | `python -m digest.digest_runner` |
| `graph/` | `knowledge_graph.json` | Auto-built after digest run |
| `vector_db/` | ChromaDB vector embeddings | `POST /reindex` |
