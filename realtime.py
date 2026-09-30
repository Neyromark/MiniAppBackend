"""WebSocket-комнаты: одна комната на организацию (аналог room=org_<id> из Socket.IO в Advanced)."""
import asyncio
import json
import logging
from typing import Dict, List, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._rooms: Dict[int, Set[WebSocket]] = {}
        self._room_of: Dict[WebSocket, int] = {}
        self._locks: Dict[WebSocket, asyncio.Lock] = {}

    def _lock(self, ws: WebSocket) -> asyncio.Lock:
        return self._locks.setdefault(ws, asyncio.Lock())

    def join(self, org_id: int, ws: WebSocket) -> None:
        self.leave(ws)
        self._rooms.setdefault(org_id, set()).add(ws)
        self._room_of[ws] = org_id

    def active_orgs(self) -> List[int]:
        return list(self._rooms)

    def leave(self, ws: WebSocket) -> None:
        org_id = self._room_of.pop(ws, None)
        if org_id is not None:
            room = self._rooms.get(org_id)
            if room:
                room.discard(ws)
                if not room:
                    self._rooms.pop(org_id, None)
        self._locks.pop(ws, None)

    async def send(self, ws: WebSocket, message: dict) -> bool:
        try:
            async with self._lock(ws):
                await ws.send_text(json.dumps(message, ensure_ascii=False))
            return True
        except Exception:
            logger.debug("не удалось отправить сообщение, отключаем сокет", exc_info=True)
            self.leave(ws)
            return False

    async def broadcast(self, org_id: int, message: dict, exclude: Optional[WebSocket] = None) -> None:
        targets = [ws for ws in list(self._rooms.get(org_id, ())) if ws is not exclude]
        if targets:
            await asyncio.gather(*(self.send(ws, message) for ws in targets))


manager = ConnectionManager()
