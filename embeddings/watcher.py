from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from digest.digest_runner import DigestRunner
from digest.project_loader import ProjectLoader
from embeddings.chunker import Chunker
from embeddings.embedder import Embedder
from embeddings.vector_store import VectorStore

logger = logging.getLogger(__name__)
WATCH_EXTS = {".java", ".ts", ".html", ".yml", ".yaml", ".properties", ".json"}


class _ChangeHandler(FileSystemEventHandler):
    def __init__(self, watcher: "FileWatcher"):
        self.watcher = watcher

    def on_modified(self, event):
        if not event.is_directory:
            self.watcher.mark_change(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self.watcher.mark_change(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self.watcher.mark_change(event.src_path)


class FileWatcher:
    def __init__(self, config_path: str = "./projects.yaml"):
        load_dotenv()
        self.config_path = config_path
        self.loader = ProjectLoader(config_path)
        self.runner = DigestRunner(config_path)
        self.embedder = Embedder()
        self.store = VectorStore(os.getenv("CHROMA_PATH", "./vector_db"))
        self._lock = threading.Lock()
        self._last_change = 0.0
        self._changed_file = ""
        self.observer = Observer()
        self.thread: threading.Thread | None = None

    def mark_change(self, file_path: str):
        if Path(file_path).suffix.lower() not in WATCH_EXTS:
            return
        with self._lock:
            self._last_change = time.time()
            self._changed_file = file_path
        logger.info("Change detected at %s", file_path)

    def _debounced_loop(self):
        while True:
            with self._lock:
                last = self._last_change
                changed_file = self._changed_file
            if last and changed_file and (time.time() - last) >= 2.0:
                self._process_change(changed_file)
                with self._lock:
                    if self._changed_file == changed_file:
                        self._last_change = 0.0
                        self._changed_file = ""
            time.sleep(0.4)

    def _process_change(self, changed_file: str):
        owner = self.loader.resolve_owner(changed_file)
        if not owner:
            return
        logger.info("Re-indexing project %s due to %s", owner.name, changed_file)
        self.runner.run_single(owner.name)
        self.store.delete_project(owner.name)
        root = Path(__file__).resolve().parents[1]
        chunks = [c for c in Chunker(str(root)).build_chunks() if c["metadata"].get("project") == owner.name]
        self.store.upsert(self.embedder.embed_chunks(chunks))

    def start_background(self):
        handler = _ChangeHandler(self)
        for p in self.loader.list_projects():
            self.observer.schedule(handler, p.path, recursive=True)
        self.observer.start()
        self.thread = threading.Thread(target=self._debounced_loop, daemon=True)
        self.thread.start()
