from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import ollama
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from agent.agent_core import AgentCore
from agent.loop import _PLAN_SENTINEL, _PLAN_SENTINEL_END
from agent.session_store import SessionStore
from graph.graph_store import GraphStore
from api.middleware import request_logger
from api.schemas import AskRequest, AskResponse, ApplyRequest, ApplyResponse, DigestResponse, ReindexRequest, ReindexResponse, SourceReference
from digest.digest_runner import DigestRunner
from digest.project_loader import ProjectLoader
from embeddings.chunker import Chunker
from embeddings.embedder import Embedder
from embeddings.vector_store import VectorStore

load_dotenv()
ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="Local AI Agent")
app.middleware("http")(request_logger)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1", "vscode-webview://*"],
    allow_origin_regex=r"http://localhost:\d+|http://127\.0\.0\.1:\d+|vscode-webview://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent_core = AgentCore()
session_store = SessionStore()
graph_store = GraphStore(str(ROOT / "graph" / "knowledge_graph.json"))
runner = DigestRunner(os.getenv("PROJECTS_CONFIG", str(ROOT / "projects.yaml")))
store = VectorStore(os.getenv("CHROMA_PATH", "./vector_db"))
embedder = Embedder()
loader = ProjectLoader(os.getenv("PROJECTS_CONFIG", str(ROOT / "projects.yaml")))
CHAT_HTML = ROOT / "api" / "static" / "chat.html"


@app.get("/")
def root():
    return {"name": "Local AI Agent", "chat_ui": "/chat", "health": "/health"}


@app.get("/chat")
def chat_ui():
    if CHAT_HTML.exists():
        return FileResponse(CHAT_HTML)
    return {"error": "Chat UI not found", "expected_path": str(CHAT_HTML)}


@app.get("/api-catalog")
def get_api_catalog():
    """Return the OpenAPI 3.0 catalog generated during last reindex."""
    catalog_path = ROOT / "api-catalog" / "openapi.json"
    if not catalog_path.exists():
        return {"error": "API catalog not built yet. Run POST /reindex first."}
    return json.loads(catalog_path.read_text(encoding="utf-8"))


@app.get("/api-catalog/markdown")
def get_api_catalog_markdown():
    """Return the Markdown API summary table generated during last reindex."""
    from fastapi.responses import PlainTextResponse
    md_path = ROOT / "api-catalog" / "api-catalog.md"
    if not md_path.exists():
        return PlainTextResponse("API catalog not built yet. Run POST /reindex first.", status_code=404)
    return PlainTextResponse(md_path.read_text(encoding="utf-8"))


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    start = time.perf_counter()
    session = session_store.get_or_create(req.session_id)
    out = agent_core.run(req.question, req.mode, req.file_context, history=session.get_recent())
    session_store.add_turn(session.session_id, req.question, out["answer"])
    sources = [
        SourceReference(
            file_path=s.get("metadata", {}).get("file_path", "digest"),
            project=s.get("metadata", {}).get("project", "unknown"),
            type=s.get("metadata", {}).get("type", "unknown"),
            preview=(s.get("content", "") or "")[:100],
        )
        for s in out.get("sources", [])
    ]
    return AskResponse(
        answer=out["answer"],
        mode=out["mode"],
        sources=sources,
        duration_ms=int((time.perf_counter() - start) * 1000),
        session_id=session.session_id,
    )


@app.post("/ask/stream")
def ask_stream(req: AskRequest):
    session = session_store.get_or_create(req.session_id)
    mode = req.mode or agent_core.detect_mode(req.question)
    history = session.get_recent()

    def event_stream():
        yield f"event: session\ndata: {session.session_id}\n\n"

        tokens: list[str] = []
        try:
            for chunk in agent_core.stream_run(req.question, mode, req.file_context, history):
                # Intercept plan sentinel emitted by AgentLoop before synthesis tokens
                if chunk.startswith(_PLAN_SENTINEL) and _PLAN_SENTINEL_END in chunk:
                    plan_json = chunk[len(_PLAN_SENTINEL): chunk.index(_PLAN_SENTINEL_END)]
                    yield f"event: plan\ndata: {plan_json}\n\n"
                else:
                    tokens.append(chunk)
                    yield f"data: {chunk}\n\n"
        finally:
            full_answer = "".join(tokens)
            if full_answer:
                session_store.add_turn(session.session_id, req.question, full_answer)

        yield f"event: done\ndata: {session.session_id}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.delete("/session/{session_id}")
def clear_session(session_id: str):
    session_store.clear(session_id)
    return {"status": "cleared", "session_id": session_id}


@app.post("/reindex", response_model=ReindexResponse)
def reindex(req: ReindexRequest):
    start = time.perf_counter()
    if req.project:
        runner.run_single(req.project)
        projects = [req.project]
    else:
        runner.run_all()
        projects = [p.name for p in loader.list_projects()]
    chunks = Chunker(str(ROOT)).build_chunks()
    embedded = embedder.embed_chunks(chunks)
    store.upsert(embedded)
    # Reload the graph store singleton and the tools graph cache after reindex
    graph_store.load()
    try:
        from agent.tools import _reload_graph
        _reload_graph()
    except Exception:
        pass
    return ReindexResponse(
        status="ok",
        projects_indexed=projects,
        chunks_created=len(embedded),
        duration_ms=int((time.perf_counter() - start) * 1000),
    )


@app.get("/digest", response_model=DigestResponse)
def digest_summary():
    digests = sorted((ROOT / "digests").glob("*.digest.json"))
    projects = []
    endpoints = 0
    entities = 0
    last = datetime.now(timezone.utc).isoformat()
    for f in digests:
        data = json.loads(f.read_text(encoding="utf-8"))
        if "project" in data:
            projects.append(data["project"])
        endpoints += len(data.get("endpoints", []))
        entities += len(data.get("entities", []))
        last = max(last, data.get("created_at", last))
    return DigestResponse(projects=projects, total_endpoints=endpoints, total_entities=entities, last_digest_at=last)


@app.get("/health")
def health():
    ollama_ok = False
    chroma_ok = False
    try:
        client = ollama.Client(host=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
        client.list()
        ollama_ok = True
    except Exception:
        pass
    try:
        store.get_stats()
        chroma_ok = True
    except Exception:
        pass
    return {
        "status": "ok" if ollama_ok and chroma_ok else "degraded",
        "ollama": ollama_ok,
        "chromadb": chroma_ok,
        "model": os.getenv("OLLAMA_MODEL", "deepseek-coder-v2"),
    }


@app.post("/apply", response_model=ApplyResponse)
def apply_diff(req: ApplyRequest):
    """
    Apply a unified diff (or multi-file patch) to the registered codebases.
    All target paths are validated against registered project roots before writing.
    """
    from agent.code_gen import apply_raw_diff

    allowed_roots = [Path(p.path).resolve() for p in loader.list_projects()]

    # Optional project root hint
    project_root: Path | None = None
    if req.project:
        for p in loader.list_projects():
            if p.name == req.project:
                project_root = Path(p.path).resolve()
                break

    ok, message, modified_paths = apply_raw_diff(req.diff, allowed_roots, project_root)

    files_modified = [p for p in modified_paths]
    files_created: list[str] = []

    if ok:
        return ApplyResponse(
            status="ok",
            files_modified=files_modified,
            files_created=files_created,
        )
    return ApplyResponse(status="error", error=message)


@app.post("/apply/file", response_model=ApplyResponse)
def apply_file(body: dict):
    """
    Create or overwrite a single file. Expects {path, content, project?}.
    Used when the LLM generates a complete new file rather than a diff.
    """
    from agent.code_gen import FileChange, apply_change

    path = body.get("path", "")
    content = body.get("content", "")
    project_hint = body.get("project")

    if not path or not content:
        return ApplyResponse(status="error", error="Both 'path' and 'content' are required")

    allowed_roots = [Path(p.path).resolve() for p in loader.list_projects()]
    project_root: Path | None = None
    if project_hint:
        for p in loader.list_projects():
            if p.name == project_hint:
                project_root = Path(p.path).resolve()
                break

    change = FileChange(action="create", path=path, content=content)
    ok, message = apply_change(change, allowed_roots, project_root)

    if ok:
        return ApplyResponse(status="ok", files_created=[path])
    return ApplyResponse(status="error", error=message)


@app.get("/graph")
def graph_data():
    """Return the full knowledge graph JSON for visualization or inspection."""
    graph_file = ROOT / "graph" / "knowledge_graph.json"
    if not graph_file.exists():
        return {"error": "Graph not built yet. Run /reindex first.", "nodes": {}, "edges": []}
    return json.loads(graph_file.read_text(encoding="utf-8"))


@app.get("/graph/summary")
def graph_summary():
    """Return a human-readable summary of the knowledge graph."""
    return {"summary": graph_store.summary(), "stats": graph_store.stats()}


@app.get("/graph/trace")
def graph_trace(q: str = ""):
    """Trace a request path through the graph. ?q=/api/orders or ?q=GET /api/orders"""
    if not q:
        return {"error": "Provide ?q=<endpoint_path>"}
    return {"result": graph_store.trace_request(q)}


@app.get("/graph/callers")
def graph_callers(q: str = ""):
    """Find callers of a node. ?q=OrderService or ?q=/api/orders"""
    if not q:
        return {"error": "Provide ?q=<class_or_path>"}
    return {"result": graph_store.find_callers(q)}


@app.get("/graph/impact")
def graph_impact(q: str = ""):
    """BFS impact analysis. ?q=Order or ?q=OrderService"""
    if not q:
        return {"error": "Provide ?q=<class_or_entity_name>"}
    return {"result": graph_store.impact_graph(q)}


@app.get("/projects")
def projects():
    out = []
    for p in loader.list_projects():
        out.append({"name": p.name, "type": p.type, "path": p.path, "exists": Path(p.path).exists()})
    return out
