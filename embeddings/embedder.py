from __future__ import annotations

import os
import time

import ollama
from dotenv import load_dotenv
from tqdm import tqdm


class Embedder:
    def __init__(self):
        load_dotenv()
        self.host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self.client = ollama.Client(host=self.host)

    def embed_chunks(self, chunks: list[dict]) -> list[dict]:
        for i in tqdm(range(0, len(chunks), 10), desc="Embedding"):
            batch = chunks[i : i + 10]
            for chunk in batch:
                chunk["embedding"] = self._embed_with_retry(chunk["content"])
        return chunks

    def embed_query(self, query: str) -> list[float]:
        res = self.client.embeddings(model=self.model, prompt=query)
        return res["embedding"]

    def _embed_with_retry(self, content: str, retries: int = 3) -> list[float]:
        last_err = None
        for attempt in range(retries):
            try:
                res = self.client.embeddings(model=self.model, prompt=content)
                return res["embedding"]
            except Exception as exc:
                last_err = exc
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"Embedding failed after {retries} retries: {last_err}")
