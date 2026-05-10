from __future__ import annotations

from collections import Counter, defaultdict

import chromadb


class VectorStore:
    def __init__(self, persist_path: str):
        self.client = chromadb.PersistentClient(path=persist_path)
        self.collection = self.client.get_or_create_collection(
            name="codebase", metadata={"hnsw:space": "cosine"}
        )

    def upsert(self, chunks: list[dict]):
        if not chunks:
            return
        self.collection.upsert(
            ids=[c["id"] for c in chunks],
            documents=[c["content"] for c in chunks],
            metadatas=[c["metadata"] for c in chunks],
            embeddings=[c["embedding"] for c in chunks],
        )

    def query(self, query_embedding: list[float], n_results: int = 8, filters: dict | None = None) -> list[dict]:
        args = {"query_embeddings": [query_embedding], "n_results": n_results}
        if filters:
            args["where"] = filters
        out = self.collection.query(**args)
        docs = out.get("documents", [[]])[0]
        metas = out.get("metadatas", [[]])[0]
        dists = out.get("distances", [[]])[0]
        return [{"content": d, "metadata": m, "distance": dist} for d, m, dist in zip(docs, metas, dists)]

    def delete_project(self, project_name: str):
        self.collection.delete(where={"project": project_name})

    def get_stats(self) -> dict:
        all_data = self.collection.get(include=["metadatas"])
        metas = all_data.get("metadatas", [])
        by_project = Counter(m.get("project", "unknown") for m in metas)
        by_type = Counter(m.get("type", "unknown") for m in metas)
        return {
            "total_chunks": len(metas),
            "count_per_project": dict(by_project),
            "count_per_type": dict(by_type),
        }
