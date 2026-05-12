from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str
    mode: Optional[str] = None
    file_context: Optional[str] = None
    project_filter: Optional[str] = None
    session_id: Optional[str] = None


class SourceReference(BaseModel):
    file_path: str
    project: str
    type: str
    preview: str


class AskResponse(BaseModel):
    answer: str
    mode: str
    sources: list[SourceReference] = Field(default_factory=list)
    duration_ms: int
    session_id: str


class ReindexRequest(BaseModel):
    project: Optional[str] = None


class ReindexResponse(BaseModel):
    status: str
    projects_indexed: list[str]
    chunks_created: int
    duration_ms: int


class DigestResponse(BaseModel):
    projects: list[str]
    total_endpoints: int
    total_entities: int
    last_digest_at: str


class ApplyRequest(BaseModel):
    diff: str                         # raw unified diff text (one or more file hunks)
    project: Optional[str] = None    # optional hint — which project root to resolve against


class ApplyResponse(BaseModel):
    status: str                                    # "ok" | "partial" | "error"
    files_modified: list[str] = Field(default_factory=list)
    files_created: list[str] = Field(default_factory=list)
    error: Optional[str] = None
