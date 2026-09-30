"""
Сторож внешних изменений: бот пишет в ту же БД напрямую (взял/начал/завершил задачу, назначил и т.д.),
а мини-апп об этом не знает. Раз в WATCH_INTERVAL_SECONDS считаем «отпечаток» данных организации;
если он изменился без нашего участия — рассылаем клиентам data_changed (reason="external").
"""
import hashlib
from datetime import date, datetime, time, timedelta
from typing import Dict

from sqlalchemy import or_
from sqlalchemy.orm import Session

from models import Member, Task, TaskAvailability, TaskInstance, User
from save_config import settings

# org_id → последний известный отпечаток
LAST: Dict[int, str] = {}


def fingerprint(session: Session, org_id: int) -> str:
    since = datetime.combine(date.today() - timedelta(days=settings.SNAPSHOT_PAST_DAYS), time.min)
    h = hashlib.sha1()

    instances = (
        session.query(
            TaskInstance.instance_id, TaskInstance.task_id, TaskInstance.assignee_user_id,
            TaskInstance.planned_start, TaskInstance.planned_end,
            TaskInstance.actual_start, TaskInstance.actual_end, TaskInstance.is_taken,
            Task.title, Task.user_id, Task.is_active, Task.check_list_template,
        )
        .join(Task, Task.task_id == TaskInstance.task_id)
        .filter(Task.organization_id == org_id,
                or_(TaskInstance.planned_end.is_(None), TaskInstance.planned_end >= since))
        .order_by(TaskInstance.instance_id)
        .all()
    )
    for row in instances:
        h.update(repr(tuple(row)).encode("utf-8"))

    h.update(b"|members|")
    members = (
        session.query(Member.member_id, Member.user_id, Member.role_id, User.user_name, User.last_name)
        .join(User, User.user_id == Member.user_id)
        .filter(Member.organization_id == org_id)
        .order_by(Member.member_id)
        .all()
    )
    for row in members:
        h.update(repr(tuple(row)).encode("utf-8"))

    h.update(b"|availability|")
    avail = (
        session.query(TaskAvailability.task_id, TaskAvailability.user_id)
        .join(Task, Task.task_id == TaskAvailability.task_id)
        .filter(Task.organization_id == org_id)
        .order_by(TaskAvailability.task_id, TaskAvailability.user_id)
        .all()
    )
    for row in avail:
        h.update(repr(tuple(row)).encode("utf-8"))
    return h.hexdigest()


def baseline(session: Session, org_id: int) -> None:
    """Запомнить отпечаток, только если его ещё нет (не прячем чужие изменения от подключённых клиентов)."""
    if org_id not in LAST:
        LAST[org_id] = fingerprint(session, org_id)


def remember(session: Session, org_id: int) -> None:
    """После НАШЕЙ записи: принять текущее состояние за известное (клиенты и так получат data_changed)."""
    LAST[org_id] = fingerprint(session, org_id)


def forget_inactive(active_org_ids) -> None:
    for org_id in [o for o in LAST if o not in set(active_org_ids)]:
        del LAST[org_id]
