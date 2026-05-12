from __future__ import annotations

import uuid
from datetime import datetime, timezone
from threading import Lock


class ConversationSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.history: list[dict] = []  # [{role: "user"|"assistant", content: str}]
        self.created_at: datetime = datetime.now(timezone.utc)
        self.last_active: datetime = self.created_at

    def add_turn(self, question: str, answer: str) -> None:
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        self.last_active = datetime.now(timezone.utc)

    def get_recent(self, max_turns: int = 6) -> list[dict]:
        """Return the last max_turns complete exchanges (user+assistant pairs)."""
        # Each exchange = 2 messages; take the tail
        tail = self.history[-(max_turns * 2):]
        return tail

    def age_minutes(self) -> float:
        delta = datetime.now(timezone.utc) - self.last_active
        return delta.total_seconds() / 60


class SessionStore:
    def __init__(self, ttl_minutes: int = 120, max_sessions: int = 200):
        self._sessions: dict[str, ConversationSession] = {}
        self._lock = Lock()
        self.ttl_minutes = ttl_minutes
        self.max_sessions = max_sessions

    def get_or_create(self, session_id: str | None) -> ConversationSession:
        with self._lock:
            self._cleanup()
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                session.last_active = datetime.now(timezone.utc)
                return session
            new_id = session_id or str(uuid.uuid4())
            session = ConversationSession(new_id)
            self._sessions[new_id] = session
            return session

    def add_turn(self, session_id: str, question: str, answer: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id].add_turn(question, answer)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _cleanup(self) -> None:
        expired = [sid for sid, s in self._sessions.items() if s.age_minutes() > self.ttl_minutes]
        for sid in expired:
            del self._sessions[sid]
        # If still over limit, evict oldest
        if len(self._sessions) > self.max_sessions:
            oldest = sorted(self._sessions.items(), key=lambda x: x[1].last_active)
            for sid, _ in oldest[: len(self._sessions) - self.max_sessions]:
                del self._sessions[sid]
