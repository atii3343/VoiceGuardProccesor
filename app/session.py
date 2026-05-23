"""Tracks active WebSocket sessions."""
import uuid
from fastapi import WebSocket


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, WebSocket] = {}

    def add(self, ws: WebSocket) -> str:
        sid = uuid.uuid4().hex[:12]
        self._sessions[sid] = ws
        return sid

    def remove(self, sid: str) -> None:
        self._sessions.pop(sid, None)

    def count(self) -> int:
        return len(self._sessions)
