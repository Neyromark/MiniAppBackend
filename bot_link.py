"""
Канал «мини-апп ⇄ чат-бот» по WebSocket (бот подключается к /ws/bot как клиент).

Мини-апп → бот   {"type":"notify","id":N,"maxId":123,"text":"..."}     бот отправляет сообщение в MAX
бот → мини-апп   {"type":"ack","id":N,"ok":true|false,"error":"..."}   подтверждение доставки
бот → мини-апп   {"type":"event","reason":"changed","orgIds":[1]|null} бот изменил задачи (назначил/взял/завершил…)

Надёжность: уведомления сначала пишутся в таблицу Bot_Notification (в транзакции самого действия), а
сюда лишь доставляются; sent_at ставится только после ack бота. Пока бот не подключён, они ждут в
очереди и уходят при его подключении. Бот в БД мини-аппа за уведомлениями НЕ ходит.
"""
import asyncio
import hmac
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from fastapi import WebSocket

import watcher
from database import db
from models import BotNotification, User
from realtime import manager
from save_config import settings
from services import LOCK

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BATCH = 50
STALE_AFTER = timedelta(days=2)      # старше — уже неактуально, не отправляем
ACK_TIMEOUT = 15.0
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _fetch() -> List[Dict[str, Any]]:
    cutoff = datetime.now() - STALE_AFTER
    with db.session() as session:
        rows = (
            session.query(BotNotification.notification_id, BotNotification.text, User.max_id)
            .join(User, User.user_id == BotNotification.user_id)
            .filter(BotNotification.sent_at.is_(None),
                    BotNotification.attempts < MAX_ATTEMPTS,
                    BotNotification.created_at >= cutoff)
            .order_by(BotNotification.notification_id)
            .limit(BATCH)
            .all()
        )
    return [{"id": i, "text": t, "max_id": m} for i, t, m in rows]


def _mark(notification_id: int, ok: bool, error: Optional[str] = None, final: bool = False) -> None:
    with db.session() as session:
        n = session.get(BotNotification, notification_id)
        if n is None:
            return
        n.attempts = MAX_ATTEMPTS if final else (n.attempts or 0) + 1
        n.last_error = error[:300] if error else None
        if ok:
            n.sent_at = datetime.now()


def _remember(org_ids: List[int]) -> None:
    """Принять состояние БД за известное: клиенты и так получат data_changed от события бота."""
    with LOCK, db.session() as session:
        for org_id in org_ids:
            watcher.remember(session, org_id)


class BotLink:
    def __init__(self) -> None:
        self._conns: Set[WebSocket] = set()
        self._acks: Dict[int, asyncio.Future] = {}
        self._flush_lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        return bool(self._conns)

    # ---------- подключение ----------
    @staticmethod
    def authorized(ws: WebSocket) -> bool:
        token = settings.BRIDGE_TOKEN
        if token:
            given = ws.headers.get("authorization", "")
            return hmac.compare_digest(given.encode(), f"Bearer {token}".encode())
        # токен не задан — пускаем только с этого же компьютера (режим разработки)
        return bool(ws.client) and ws.client.host in LOOPBACK

    def attach(self, ws: WebSocket) -> None:
        self._conns.add(ws)
        logger.info("Бот подключён к мини-аппу (%s)", ws.client)
        self.request_flush()

    def detach(self, ws: WebSocket) -> None:
        if ws in self._conns:
            self._conns.discard(ws)
            logger.info("Бот отключён от мини-аппа")

    # ---------- мини-апп → бот ----------
    def request_flush(self) -> None:
        """Неблокирующий запуск доставки (вызывается после каждого изменяющего действия)."""
        if not self._conns:
            return
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self.flush())

    async def flush(self) -> None:
        if not self._conns:
            return
        async with self._flush_lock:
            for _ in range(20):                       # не более 20 пачек за вызов
                if not self._conns:
                    return
                batch = await asyncio.to_thread(_fetch)
                if not batch:
                    return
                failed = False
                for item in batch:
                    ws = next(iter(self._conns), None)
                    if ws is None:
                        return
                    if not item["max_id"]:
                        await asyncio.to_thread(_mark, item["id"], False, "у пользователя нет MaxId", True)
                        continue
                    ok, error = await self._deliver(ws, item)
                    if error == "disconnected":       # бот пропал — не считаем попыткой
                        return
                    await asyncio.to_thread(_mark, item["id"], ok, error)
                    failed = failed or not ok
                if failed or len(batch) < BATCH:      # повтор неудачных — по таймеру, без «горячего» цикла
                    return

    async def _deliver(self, ws: WebSocket, item: Dict[str, Any]):
        fut = asyncio.get_running_loop().create_future()
        self._acks[item["id"]] = fut
        try:
            await ws.send_text(json.dumps(
                {"type": "notify", "id": item["id"], "maxId": item["max_id"], "text": item["text"]},
                ensure_ascii=False))
            ack = await asyncio.wait_for(fut, ACK_TIMEOUT)
            return bool(ack.get("ok")), (ack.get("error") if not ack.get("ok") else None)
        except asyncio.TimeoutError:
            return False, "бот не подтвердил доставку"
        except Exception:
            self.detach(ws)
            return False, "disconnected"
        finally:
            self._acks.pop(item["id"], None)

    # ---------- бот → мини-апп ----------
    async def handle(self, ws: WebSocket, msg: Dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "ack":
            fut = self._acks.get(msg.get("id"))
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif kind == "event":
            await self._on_event(msg)
        elif kind == "ping":
            await ws.send_text('{"type":"pong"}')

    async def _on_event(self, msg: Dict[str, Any]) -> None:
        active = set(manager.active_orgs())
        raw = msg.get("orgIds")
        if isinstance(raw, list):
            orgs = sorted({o for o in raw if isinstance(o, int) and o in active})
        else:
            orgs = sorted(active)                     # организация неизвестна — обновляем всех
        if not orgs:
            return
        await asyncio.to_thread(_remember, orgs)
        for org_id in orgs:
            await manager.broadcast(org_id, {"type": "data_changed",
                                             "reason": f"bot.{msg.get('reason') or 'changed'}"})


bot_link = BotLink()
