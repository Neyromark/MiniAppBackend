"""
CurrentMiniAppBackend — FastAPI + WebSocket + SQLAlchemy ORM.

Единственный канал общения с фронтендом — WebSocket /ws (JSON-сообщения):
  клиент → сервер : {"id": 7, "action": "task.create", "payload": {...}}
  сервер → клиент : {"type": "response", "id": 7, "ok": true, "data": {...}}
                    {"type": "response", "id": 7, "ok": false, "error": "conflict", "message": "...", "details": ...}
  сервер → клиенты: {"type": "data_changed", "reason": "task.create", "by": <userId>}   (всем в организации)

Первое сообщение клиента — "hello" с initData; в ответ приходит полный снимок данных.
После data_changed клиенты запрашивают "snapshot" заново (у каждого своя видимость по ролям).

Запуск:  python app.py
"""
import asyncio
import contextlib
import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

import services

from save_config import settings
from database import db
from errors import ApiError
from permissions import Actor, authenticate, load_actor
import watcher
from bot_link import bot_link
from models import Organization
from realtime import manager
from seed import seed_demo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("miniapp")

# action → (обработчик, меняет ли данные)
Handler = Callable[[Any, Actor, Dict[str, Any]], Dict[str, Any]]
ACTIONS: Dict[str, Tuple[Handler, bool]] = {
    "snapshot": (lambda s, a, p: services.get_snapshot(s, a), False),
    "task.create": (services.create_task, True),
    "task.update": (services.update_task, True),
    "task.delete": (services.delete_task, True),
    "pool.take": (services.take_pool_task, True),
    "pool.return": (services.return_pool_task, True),
    "deadline.create": (services.create_deadline, True),
    "deadline.accept": (services.accept_deadline, True),
    "deadline.delete": (services.delete_deadline, True),
}


def _run_sync(user_ref: Tuple[int, int], action: str, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Выполняется в потоке. Одна транзакция на действие; блокировка делает check-then-write атомарным."""
    handler, _ = ACTIONS[action]
    with services.LOCK:
        with db.session() as session:
            actor = load_actor(session, *user_ref)
            if actor is None:
                raise ApiError("unauthorized", "Пользователь не найден. Перезагрузите страницу")
            result, org_id = handler(session, actor, payload), actor.org_id
        if ACTIONS[action][1]:                   # изменение уже зафиксировано: наши правки — не «внешние»
            with db.session() as session:
                watcher.remember(session, org_id)
        return result, org_id


def _hello_sync(init_data: Any) -> Tuple[Dict[str, Any], Tuple[int, int], int]:
    with services.LOCK, db.session() as session:
        actor = authenticate(session, init_data)
        snapshot = services.get_snapshot(session, actor)
        session.flush()
        watcher.baseline(session, actor.org_id)
        return snapshot, (actor.user_id, actor.member_id), actor.org_id


async def _maintenance_loop() -> None:
    """Раз в 30 минут досоздаём экземпляры регулярных задач и оповещаем клиентов."""
    while True:
        await asyncio.sleep(30 * 60)
        try:
            def work():
                with services.LOCK, db.session() as session:
                    changed = []
                    for (org_id,) in session.query(Organization.organization_id).all():
                        if services.materialize_regular(session, org_id):
                            changed.append(org_id)
                    for org_id in changed:
                        session.flush()
                        watcher.remember(session, org_id)
                    return changed
            bot_link.request_flush()                      # повтор неудачных уведомлений
            for org_id in await asyncio.to_thread(work):
                await manager.broadcast(org_id, {"type": "data_changed", "reason": "regular.materialized"})
        except Exception:
            logger.exception("ошибка фонового обслуживания")


def _watch_once(org_ids):
    """Организации, данные которых изменились мимо мини-аппа (например, бот). Выполняется в потоке."""
    changed = []
    with services.LOCK, db.session() as session:
        for org_id in org_ids:
            current = watcher.fingerprint(session, org_id)
            if org_id not in watcher.LAST:
                watcher.LAST[org_id] = current
            elif watcher.LAST[org_id] != current:
                watcher.LAST[org_id] = current
                changed.append(org_id)
    return changed


async def _watch_loop() -> None:
    """Быстрый опрос БД: изменения от бота доходят до открытых мини-аппов за пару секунд."""
    while True:
        await asyncio.sleep(settings.WATCH_INTERVAL_SECONDS)
        try:
            org_ids = manager.active_orgs()
            watcher.forget_inactive(org_ids)
            if not org_ids:
                continue
            for org_id in await asyncio.to_thread(_watch_once, org_ids):
                await manager.broadcast(org_id, {"type": "data_changed", "reason": "external"})
        except Exception:
            logger.exception("ошибка опроса внешних изменений")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.check_auth_config()
    if settings.DEV_AUTH:
        logger.warning("ВНИМАНИЕ: DEV_AUTH=true — вход по неподписанному user.id. Не используйте в продакшене!")
    db.connect()
    db.create_all()
    if settings.SEED_DEMO:
        with db.session() as session:
            seed_demo(session)
    tasks = [asyncio.create_task(_maintenance_loop()), asyncio.create_task(_watch_loop())]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        db.dispose()


app = FastAPI(title="CurrentMiniAppBackend", lifespan=lifespan)


@app.get("/health")
async def health():
    return JSONResponse({"ok": True})


def _error_message(exc: ApiError, req_id: Any) -> Dict[str, Any]:
    msg = {"type": "response", "id": req_id, "ok": False, "error": exc.code, "message": exc.message}
    if exc.details is not None:
        msg["details"] = exc.details
    return msg


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    user_ref: Optional[Tuple[int, int]] = None
    org_id: Optional[int] = None
    try:
        while True:
            raw = await ws.receive_text()
            req_id: Any = None
            try:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    raise ApiError("bad_request", "Сообщение не является JSON")
                if not isinstance(msg, dict):
                    raise ApiError("bad_request", "Ожидался JSON-объект")
                req_id = msg.get("id")
                action = msg.get("action")
                payload = msg.get("payload") or {}
                if not isinstance(payload, dict):
                    raise ApiError("bad_request", "payload должен быть объектом")

                if action == "ping":
                    await manager.send(ws, {"type": "response", "id": req_id, "ok": True, "data": {"pong": True}})
                    continue

                if action == "hello":
                    data, user_ref, org_id = await asyncio.to_thread(_hello_sync, msg.get("initData"))
                    manager.join(org_id, ws)
                    await manager.send(ws, {"type": "response", "id": req_id, "ok": True, "data": data})
                    continue

                if action not in ACTIONS:
                    raise ApiError("bad_request", f"Неизвестное действие: {action}")
                if user_ref is None:
                    raise ApiError("unauthorized", "Сначала отправьте hello")

                data, actor_org = await asyncio.to_thread(_run_sync, user_ref, action, payload)
                await manager.send(ws, {"type": "response", "id": req_id, "ok": True, "data": data})
                if ACTIONS[action][1]:
                    await manager.broadcast(
                        actor_org, {"type": "data_changed", "reason": action, "by": user_ref[0]})
                    bot_link.request_flush()           # уведомления боту (если он подключён)
            except ApiError as exc:
                await manager.send(ws, _error_message(exc, req_id))
            except WebSocketDisconnect:
                raise
            except Exception:
                logger.exception("необработанная ошибка (action из сообщения: %s)", raw[:200])
                await manager.send(ws, {"type": "response", "id": req_id, "ok": False,
                                        "error": "server_error", "message": "Внутренняя ошибка сервера"})
    except WebSocketDisconnect:
        pass
    finally:
        manager.leave(ws)


@app.websocket("/ws/bot")
async def bot_socket(ws: WebSocket):
    """Служебный канал для CurrentChatBot (не для браузеров)."""
    if not bot_link.authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    bot_link.attach(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                await bot_link.handle(ws, msg)
    except WebSocketDisconnect:
        pass
    finally:
        bot_link.detach(ws)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="info")
