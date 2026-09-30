"""
Исходящие уведомления боту (transactional outbox).

Запись в Bot_Notification делается В ТОЙ ЖЕ транзакции, что и изменение задачи: если действие
откатилось — уведомления тоже нет; если зафиксировалось — уведомление не потеряется, даже
если бот сейчас выключен. Доставляет их боту bot_link.py по WebSocket (/ws/bot); бот отправляет сообщения в MAX и подтверждает (ack).
"""
from typing import Iterable

from sqlalchemy.orm import Session

from models import BotNotification
from save_config import settings

MAX_TEXT = 3500


def enqueue(session: Session, user_ids: Iterable[int], text: str, exclude: Iterable[int] = ()) -> int:
    """Поставить сообщение в очередь бота для каждого получателя (без дублей и без исключённых)."""
    if not settings.notify_enabled():
        return 0
    skip = set(exclude)
    seen = set()
    count = 0
    for uid in user_ids:
        if uid is None or uid in skip or uid in seen:
            continue
        seen.add(uid)
        session.add(BotNotification(user_id=uid, text=text[:MAX_TEXT], attempts=0))
        count += 1
    return count
