# Local AI Agent

Private, offline, project-specific AI agent for Angular + Spring Boot microservices using Ollama, ChromaDB, FastAPI, and a VS Code extension.

## What Was Built

### Project Foundation

- Root project created at `local-ai-agent/` with full multi-stage architecture.
- Configuration and setup files added:
  - `projects.yaml` for project registry (frontend + services)
  - `.env` for runtime configuration (`OLLAMA_HOST`, models, Chroma path, API port)
  - `requirements.txt` with FastAPI, LangChain, Ollama, Chroma, parsing, watcher deps
  - `README.md` documentation

### Stage 1: Digest Engine (`digest/`)

- `digest/models.py`
  - Implemented all digest schemas using Pydantic:
    - `EndpointDigest`, `EntityDigest`, `FeignClientDigest`, `EventDigest`
    - `ServiceDigest`, `AngularComponentDigest`, `AngularServiceDigest`, `AngularDigest`
    - `ApiContract`, `MasterDigest`
- `digest/project_loader.py`
  - Loads `projects.yaml`
  - Lists all registered projects
  - Finds a project by name
  - Resolves owner project for a changed file path
- `digest/springboot_parser.py`
  - Parses Spring Boot codebase for:
    - Controllers + endpoint mappings (`GET/POST/PUT/DELETE/PATCH`)
    - Request/response types and auth annotations/roles
    - JPA entities, table names, fields, relationships
    - DTO class discovery (`DTO`, `Request`, `Response`)
    - Feign clients and mapped calls
    - Kafka/Rabbit consume/produce patterns
    - Security hints (`JWT`, CORS, permit/authenticated paths)
    - Application config hints from `application.yml/.yaml/.properties`
- `digest/angular_parser.py`
  - Parses Angular project for:
    - Modules (`*.module.ts`)
    - Components (`selector`, inputs, outputs, injected services)
    - Services (HttpClient calls + dependencies)
    - Routing modules including lazy routes
    - Guards, interceptors, model/interface names
    - Environment URL keys (`apiUrl`, `baseUrl`, `apiBaseUrl`)
- `digest/master_digest_builder.py`
  - Builds cross-project system map:
    - FE->BE API contracts
    - Service dependencies (Feign + event relationships)
    - JWT auth flow (issuer/validators/interceptor)
    - Shared DTO model usage
- `digest/digest_runner.py`
  - Orchestrates full and partial digest runs:
    - `run_all()`
    - `run_single(project_name)`
    - `run_incremental(changed_file)`
  - Writes digest outputs into `digests/`
  - Rebuilds `master.digest.json`
  - Includes timing logs and fail-isolated project execution

### Stage 2: Embeddings Pipeline (`embeddings/`)

- `embeddings/chunker.py`
  - Builds chunks from digest JSON + raw source/config files
  - Uses token-aware chunk splitting (`tiktoken`, `cl100k_base`)
  - Applies overlap between sequential chunks
  - Adds normalized chunk IDs and retrieval metadata
- `embeddings/vector_store.py`
  - Wraps ChromaDB persistent collection (`codebase`)
  - Supports:
    - `upsert(chunks)`
    - `query(embedding, n_results, filters)`
    - `delete_project(project_name)`
    - `get_stats()`
- `embeddings/embedder.py`
  - Ollama embedding integration (`nomic-embed-text`)
  - Batch embedding flow with progress bar and retries
  - Query embedding support
- `embeddings/watcher.py`
  - Watchdog-based file watcher for incremental updates
  - Watches registered project paths
  - Debounces rapid changes
  - Re-digests changed project and re-embeds only that project

### Stage 3: Agent Core (`agent/`)

- `agent/prompts.py`
  - Added prompt templates for:
    - Base system behavior
    - Code Q&A
    - Code generation
    - Impact analysis
- `agent/tools.py`
  - Implemented tool functions for:
    - Global and project-scoped vector search
    - Endpoint listing from service digests
    - API contracts and service dependency queries
    - Entity schema lookup
    - Secure source file reading with project boundary validation
    - Auth flow lookup
  - Exposed as LangChain `Tool` objects
- `agent/rag_chain.py`
  - Integrates Ollama LLM + embeddings + Chroma retrieval
  - Implements:
    - `ask(question, mode, file_context)` with source metadata
    - `stream_ask(...)` for token streaming
    - `get_retriever(filters)`
- `agent/agent_core.py`
  - Adds high-level orchestration and mode routing
  - ReAct agent path for impact analysis
  - Auto mode detection:
    - `generate` for create/modify verbs
    - `impact` for impact/risk phrasing
    - `chat` fallback

### Stage 4: FastAPI Server (`api/`)

- `api/schemas.py`
  - Added request/response models:
    - `AskRequest`, `AskResponse`, `SourceReference`
    - `ReindexRequest`, `ReindexResponse`
    - `DigestResponse`
- `api/middleware.py`
  - Added request logging middleware with latency metrics
- `api/server.py`
  - Implemented API endpoints:
    - `POST /ask`
    - `POST /ask/stream` (SSE token stream)
    - `POST /reindex`
    - `GET /digest`
    - `GET /health`
    - `GET /projects`
  - Added CORS support for localhost and VS Code webview origins
  - Wired digest, chunking, embedding, vector upsert, and agent runtime

### Stage 5: VS Code Extension (`vscode-extension/`)

- `vscode-extension/package.json`
  - Extension manifest, commands, menus, sidebar webview, scripts
- `vscode-extension/tsconfig.json`
  - TypeScript compile configuration
- `vscode-extension/src/api_client.ts`
  - Backend client for:
    - `/ask`, `/ask/stream`, `/reindex`, `/health`, `/digest`
  - Includes user-facing network error handling
- `vscode-extension/src/chat_panel.ts`
  - Webview panel lifecycle + messaging
  - Streaming response rendering
  - Mode handling and source reference rendering
- `vscode-extension/src/code_actions.ts`
  - Command handlers:
    - explain selection
    - generate change
    - impact analysis
    - reindex
    - open chat
- `vscode-extension/src/inline_lens.ts`
  - CodeLens provider for TypeScript/Java context markers:
    - Angular component/service
    - Spring controller/entity
    - endpoint annotations
- `vscode-extension/src/extension.ts`
  - Activation entry point
  - Registers commands and CodeLens provider
- `vscode-extension/media/`
  - `chat.html`, `chat.css`, and `icon.svg`

### Outputs and Runtime Paths

- Digest output directory: `digests/`
- Vector database directory: `vector_db/` (created at runtime by Chroma)
- Default API server port: `8765`
- Default models:
  - LLM: `deepseek-coder-v2`
  - Embeddings: `nomic-embed-text`

### Validation Completed

- Python syntax validation completed successfully with:
  - `python3 -m compileall digest embeddings agent api`
- Current implementation is scaffolded and wired end-to-end for local/offline use.

## Quick Start

1. Pull models:
   - `ollama pull deepseek-coder-v2`
   - `ollama pull nomic-embed-text`
2. Install Python deps:
   - `pip install -r requirements.txt`
3. Update `projects.yaml` with real local paths.
4. Run full digest:
   - `python -m digest.digest_runner`
5. Start API:
   - `uvicorn api.server:app --port 8765 --reload`
6. Build extension:
   - `cd vscode-extension && npm install && npm run compile`

## Architecture

- `digest/`: structured code digest for FE + BE + master contracts.
- `embeddings/`: chunking, embedding, ChromaDB storage, watcher.
- `agent/`: prompts, tools, RAG chain, orchestrator.
- `api/`: FastAPI endpoints for ask/reindex/digest/health/projects.
- `vscode-extension/`: chat UX, commands, code lenses, API integration.
