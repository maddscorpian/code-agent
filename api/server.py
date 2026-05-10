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
from fastapi.responses import StreamingResponse

from agent.agent_core import AgentCore
from agent.rag_chain import RAGChain
from api.middleware import request_logger
from api.schemas import AskRequest, AskResponse, DigestResponse, ReindexRequest, ReindexResponse, SourceReference
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
runner = DigestRunner(os.getenv("PROJECTS_CONFIG", str(ROOT / "projects.yaml")))
store = VectorStore(os.getenv("CHROMA_PATH", "./vector_db"))
embedder = Embedder()
loader = ProjectLoader(os.getenv("PROJECTS_CONFIG", str(ROOT / "projects.yaml")))


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest):
    start = time.perf_counter()
    out = agent_core.run(req.question, req.mode, req.file_context)
    sources = [
        SourceReference(
            file_path=s.get("file_path", "digest"),
            project=s.get("project", "unknown"),
            type=s.get("type", "unknown"),
            preview="",
        )
        for s in out.get("sources", [])
    ]
    return AskResponse(
        answer=out["answer"],
        mode=out["mode"],
        sources=sources,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )


@app.post("/ask/stream")
def ask_stream(req: AskRequest):
    mode = req.mode or agent_core.detect_mode(req.question)

    def event_stream():
        for token in RAGChain().stream_ask(req.question, mode, req.file_context):
            yield f"data: {token}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


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
        ollama_ok = False
    try:
        store.get_stats()
        chroma_ok = True
    except Exception:
        chroma_ok = False
    return {
        "status": "ok" if ollama_ok and chroma_ok else "degraded",
        "ollama": ollama_ok,
        "chromadb": chroma_ok,
        "model": os.getenv("OLLAMA_MODEL", "deepseek-coder-v2"),
    }


@app.get("/projects")
def projects():
    out = []
    for p in loader.list_projects():
        out.append({"name": p.name, "type": p.type, "path": p.path, "exists": Path(p.path).exists()})
    return out
