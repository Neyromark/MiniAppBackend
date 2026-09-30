"""
Бизнес-логика мини-приложения на ORM.

Модель хранения (совместима с CurrentChatBot):
  * разовая задача      — Task + Schedule_Rule-заглушка (WeekdayMask=0) + один Task_Instance;
  * регулярная задача   — Task + Schedule_Rule (WeekdayMask != 0), экземпляры материализуются
                          на REGULAR_HORIZON_DAYS дней вперёд (идемпотентно по (TaskId, PlannedEnd));
  * задача «в пул»      — Task.UserId IS NULL, экземпляр без исполнителя (IsTaken = 0);
  * дедлайн             — Task с kind="deadline" в Task.CheckListTemplate + экземпляр с PlannedEnd;
  * «выборочный» дедлайн — UserId IS NULL + строки Task_Availability (кому можно принять).

«Двойная проверка» (как в AdvancedMiniAppBackend, только строже):
  1) валидация входных данных и прав;
  2) проверка конфликтов внутри транзакции под глобальной блокировкой записи;
  3) повторная проверка после flush() — до commit (ловит гонки с ботом, который пишет в ту же БД).
"""
import json
import re
import threading
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from save_config import settings
import notify
from errors import ApiError
from models import (Organization, ScheduleRule, Task, TaskAvailability, TaskInstance)
from permissions import (Actor, assignable_ids, org_members, role_key, visible_ids)

# Все операции (чтение+запись) сериализуются: check-then-write должен быть атомарным.
LOCK = threading.RLock()

DEFAULT_COLOR = "#7B9EFF"
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
STATUS_NEW = 1


# ============================================================
# ВСПОМОГАТЕЛЬНОЕ: разбор и сериализация
# ============================================================
def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else None


def _hm(t) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def _minutes(t) -> int:
    return t.hour * 60 + t.minute


def _bad(message: str, details: Any = None) -> ApiError:
    return ApiError("bad_request", message, details)


def _parse_date(value: Any, field: str, required: bool = True) -> Optional[date]:
    if value in (None, ""):
        if required:
            raise _bad(f"Не указана дата ({field})")
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise _bad(f"Некорректная дата ({field}): {value!r}")


def _parse_time(value: Any, field: str, required: bool = True) -> Optional[time]:
    if value in (None, ""):
        if required:
            raise _bad(f"Не указано время ({field})")
        return None
    try:
        return datetime.strptime(str(value)[:5], "%H:%M").time()
    except ValueError:
        raise _bad(f"Некорректное время ({field}): {value!r}")


def _parse_datetime(value: Any, field: str) -> datetime:
    if not value:
        raise _bad(f"Не указан срок ({field})")
    text = str(value).replace("T", " ")[:16]
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        raise _bad(f"Некорректная дата и время ({field}): {value!r}")


def _parse_int(value: Any, field: str, lo: int, hi: Optional[int] = None) -> int:
    if isinstance(value, bool):
        raise _bad(f"Некорректное число ({field})")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _bad(f"Некорректное число ({field})")
    if number < lo or (hi is not None and number > hi):
        raise _bad(f"Значение ({field}) вне диапазона")
    return number


def _int_list(value: Any, field: str) -> List[int]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise _bad(f"Ожидался список ({field})")
    result: List[int] = []
    for item in value:
        number = _parse_int(item, field, 0)
        if number not in result:
            result.append(number)
    return result


def _title(value: Any) -> str:
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        raise _bad("Укажите название")
    if len(text) > 200:
        raise _bad("Название слишком длинное (максимум 200 символов)")
    return text


def _color(value: Any) -> str:
    return value if isinstance(value, str) and _COLOR_RE.match(value) else DEFAULT_COLOR


def _js_days_to_mask(days: Sequence[int]) -> int:
    """JS getDay(): Вс=0, Пн=1 … Сб=6  →  маска бота: Пн=1, Вт=2 … Вс=64."""
    mask = 0
    for d in days:
        mask |= 1 << ((d + 6) % 7)
    return mask


def _mask_to_js_days(mask: int) -> List[int]:
    return [(bit + 1) % 7 for bit in range(7) if mask & (1 << bit)]


def _meta_json(kind: str, color: str, important: bool) -> str:
    return json.dumps({"kind": kind, "color": color, "important": bool(important)}, ensure_ascii=False)


def _meta(task: Task) -> Dict[str, Any]:
    data: Any = {}
    if task.check_list_template:
        try:
            data = json.loads(task.check_list_template)
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    kind = data.get("kind")
    return {
        "kind": kind if kind in ("task", "deadline") else "task",
        "color": _color(data.get("color")),
        "important": bool(data.get("important", False)),
    }


def _is_regular(rule: Optional[ScheduleRule]) -> bool:
    return rule is not None and rule.weekday_mask != 0


def _is_pool(task: Task) -> bool:
    return task.user_id is None and not task.external_role_name


def _when_text(start: Optional[datetime], end: Optional[datetime]) -> str:
    if start and end:
        return f"{end:%d.%m.%Y}, {_hm(start)}–{_hm(end)}"
    if end:
        return f"{end:%d.%m.%Y}, срок до {_hm(end)}"
    return "без срока"


def _notify(session: Session, actor: Actor, user_ids: Iterable[Optional[int]], text: str) -> None:
    """Уведомить сотрудников через бота. Автора не уведомляем, кроме случая, когда он единственный получатель."""
    recipients = {u for u in user_ids if u is not None}
    exclude = [] if recipients == {actor.user_id} else [actor.user_id]
    notify.enqueue(session, sorted(recipients),
                   f"{text}\nКто: {actor.full_name} (мини-приложение)", exclude=exclude)


def _require_edit(actor: Actor) -> None:
    if not actor.can_edit:
        raise ApiError("forbidden", "Недостаточно прав: создавать и менять задачи могут владелец и администратор")


# ============================================================
# ЗАГРУЗКА ЗАПИСЕЙ С ПРОВЕРКОЙ ОРГАНИЗАЦИИ
# ============================================================
def _load_instance(session: Session, actor: Actor, instance_id: Any) -> Tuple[TaskInstance, Task, Optional[ScheduleRule]]:
    iid = _parse_int(instance_id, "instanceId", 1)
    row = (
        session.query(TaskInstance, Task, ScheduleRule)
        .join(Task, Task.task_id == TaskInstance.task_id)
        .outerjoin(ScheduleRule, ScheduleRule.rule_id == Task.schedule_rule_id)
        .filter(TaskInstance.instance_id == iid)
        .first()
    )
    if row is None or row[1].organization_id != actor.org_id or not row[1].is_active:
        raise ApiError("not_found", "Задача не найдена")
    return row


def _check_can_modify(session: Session, actor: Actor, inst: TaskInstance) -> None:
    """Менять/удалять можно экземпляры тех, кому актёр вправе назначать (и пул)."""
    _require_edit(actor)
    if inst.assignee_user_id is None:
        return
    members = org_members(session, actor.org_id)
    if inst.assignee_user_id not in assignable_ids(actor, members):
        raise ApiError("forbidden", "Нельзя изменять задачи этого сотрудника")


# ============================================================
# КОНФЛИКТЫ
# ============================================================
def _overlapping(
    session: Session, user_id: int, start: datetime, end: datetime,
    exclude_ids: Iterable[int] = (), exclude_regular: bool = False,
) -> List[Tuple[TaskInstance, Task]]:
    query = (
        session.query(TaskInstance, Task)
        .join(Task, Task.task_id == TaskInstance.task_id)
        .filter(
            Task.is_active == True,  # noqa: E712
            TaskInstance.assignee_user_id == user_id,
            TaskInstance.planned_start.isnot(None),
            TaskInstance.planned_end.isnot(None),
            TaskInstance.planned_start < end,
            TaskInstance.planned_end > start,
        )
    )
    exclude = list(exclude_ids)
    if exclude:
        query = query.filter(TaskInstance.instance_id.notin_(exclude))
    if exclude_regular:
        query = (query.outerjoin(ScheduleRule, ScheduleRule.rule_id == Task.schedule_rule_id)
                 .filter(or_(ScheduleRule.rule_id.is_(None), ScheduleRule.weekday_mask == 0)))
    return query.order_by(TaskInstance.planned_start).all()


def _conflict_lines(names: Dict[int, str], found: Dict[int, List[Tuple[TaskInstance, Task]]]) -> List[str]:
    lines: List[str] = []
    for uid, pairs in found.items():
        lines.append(f"• {names.get(uid, uid)}:")
        for inst, task in pairs:
            lines.append(f"   — «{task.title}» ({_hm(inst.planned_start)} – {_hm(inst.planned_end)})")
        lines.append("")
    return lines


def _collect_conflicts(session: Session, user_ids: Iterable[int], start: datetime, end: datetime,
                       names: Dict[int, str], exclude_ids: Iterable[int] = ()) -> None:
    found: Dict[int, List[Tuple[TaskInstance, Task]]] = {}
    for uid in user_ids:
        pairs = _overlapping(session, uid, start, end, exclude_ids)
        if pairs:
            found[uid] = pairs
    if found:
        details = [
            {"userId": uid, "userName": names.get(uid, str(uid)),
             "items": [{"title": t.title, "start": _hm(i.planned_start), "end": _hm(i.planned_end)}
                       for i, t in pairs]}
            for uid, pairs in found.items()
        ]
        message = "\n".join(["Обнаружены пересечения:", ""] + _conflict_lines(names, found)).strip()
        raise ApiError("conflict", message, details)


def find_regular_rule_conflicts(
    session: Session, org_id: int, user_id: int, mask: int,
    start_t: Optional[time], deadline_t: time, start_date: date, end_date: Optional[date],
) -> List[Dict[str, str]]:
    """Пересечения новой регулярной задачи с уже существующими регулярными того же исполнителя."""
    if start_t is None or user_id is None:
        return []
    rows = (
        session.query(Task, ScheduleRule)
        .join(ScheduleRule, ScheduleRule.rule_id == Task.schedule_rule_id)
        .filter(
            Task.organization_id == org_id, Task.user_id == user_id, Task.is_active == True,  # noqa: E712
            Task.deadline_offset.isnot(None), ScheduleRule.weekday_mask != 0,
        )
        .all()
    )
    new_s, new_e = _minutes(start_t), _minutes(deadline_t)
    result = []
    for task, rule in rows:
        if not (rule.weekday_mask & mask):
            continue
        if rule.end_date is not None and rule.end_date < start_date:
            continue
        if end_date is not None and end_date < rule.start_date:
            continue
        ex_s = _minutes(rule.time)
        ex_e = ex_s + task.deadline_offset
        if new_s >= ex_e or new_e <= ex_s:
            continue
        result.append({"title": task.title or "—",
                       "start": f"{ex_s // 60 % 24:02d}:{ex_s % 60:02d}",
                       "end": f"{ex_e // 60 % 24:02d}:{ex_e % 60:02d}"})
    return result


# ============================================================
# РЕГУЛЯРНЫЕ ЗАДАЧИ: материализация экземпляров
# ============================================================
def materialize_regular(session: Session, org_id: Optional[int] = None,
                        task_ids: Optional[Iterable[int]] = None) -> List[TaskInstance]:
    """
    Создаёт недостающие Task_Instance регулярных задач от понедельника текущей недели
    до сегодня + REGULAR_HORIZON_DAYS. Идемпотентно (ключ — TaskId + PlannedEnd).
    Хранение как у бота: если есть время начала — Rule.Time = начало, DeadlineOffset = минуты до
    дедлайна; иначе Rule.Time = дедлайн, DeadlineOffset = NULL.
    """
    today = date.today()
    window_start = today - timedelta(days=today.weekday())
    window_end = today + timedelta(days=settings.REGULAR_HORIZON_DAYS)

    query = (
        session.query(Task, ScheduleRule)
        .join(ScheduleRule, ScheduleRule.rule_id == Task.schedule_rule_id)
        .filter(
            Task.is_active == True, ScheduleRule.weekday_mask != 0,  # noqa: E712
            ScheduleRule.start_date <= window_end,
            or_(ScheduleRule.end_date.is_(None), ScheduleRule.end_date >= window_start),
        )
    )
    if org_id is not None:
        query = query.filter(Task.organization_id == org_id)
    if task_ids is not None:
        ids = list(task_ids)
        if not ids:
            return []
        query = query.filter(Task.task_id.in_(ids))
    pairs = query.all()
    if not pairs:
        return []

    existing = set(
        session.query(TaskInstance.task_id, TaskInstance.planned_end)
        .filter(TaskInstance.task_id.in_([t.task_id for t, _ in pairs]),
                TaskInstance.planned_end >= datetime.combine(window_start, time.min))
        .all()
    )

    created: List[TaskInstance] = []
    for task, rule in pairs:
        day = max(window_start, rule.start_date)
        last = window_end if rule.end_date is None else min(window_end, rule.end_date)
        while day <= last:
            if rule.weekday_mask & (1 << day.weekday()):
                if task.deadline_offset is not None:
                    planned_start = datetime.combine(day, rule.time)
                    planned_end = planned_start + timedelta(minutes=task.deadline_offset)
                else:
                    planned_start = None
                    planned_end = datetime.combine(day, rule.time)
                if (task.task_id, planned_end) not in existing:
                    inst = TaskInstance(
                        task_id=task.task_id, assignee_user_id=task.user_id, status_id=STATUS_NEW,
                        planned_start=planned_start, planned_end=planned_end,
                        is_taken=task.user_id is not None,
                    )
                    session.add(inst)
                    created.append(inst)
                    existing.add((task.task_id, planned_end))
            day += timedelta(days=1)
    if created:
        session.flush()
    return created


# ============================================================
# СНИМОК ДАННЫХ ДЛЯ КЛИЕНТА
# ============================================================
def _task_dict(task: Task, rule: Optional[ScheduleRule], meta: Dict[str, Any]) -> Dict[str, Any]:
    regular = _is_regular(rule)
    data: Dict[str, Any] = {
        "id": task.task_id,
        "title": task.title,
        "description": task.description or "",
        "type": "regular" if regular else ("by_role" if task.external_role_name else "single"),
        "weight": task.weight,
        "estimated_minutes": task.estimated_minutes,
        "important": meta["important"],
        "color": meta["color"],
        "org_role": task.external_role_name,
        "is_pool": _is_pool(task),
        "assignee": task.user_id,
        "assignees": [task.user_id] if task.user_id is not None else [],
        "planned_start": None,
        "planned_end": None,
        "created_at": _fmt_dt(task.created_at),
    }
    if regular:
        if task.deadline_offset is not None:
            start_min = _minutes(rule.time)
            end_min = (start_min + task.deadline_offset) % (24 * 60)
            start_time, deadline_time = _hm(rule.time), f"{end_min // 60:02d}:{end_min % 60:02d}"
        else:
            start_time, deadline_time = None, _hm(rule.time)
        data["schedule_rule"] = {
            "deadline_time": deadline_time,
            "start_time": start_time,
            "weekdays": _mask_to_js_days(rule.weekday_mask),
            "period_start": rule.start_date.isoformat(),
            "period_end": rule.end_date.isoformat() if rule.end_date else None,
        }
    return data


def _instance_dict(inst: TaskInstance, task: Task, meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": inst.instance_id,
        "task_id": task.task_id,
        "title": task.title,
        "description": task.description or "",
        "weight": task.weight,
        "estimated_minutes": task.estimated_minutes,
        "assignee": inst.assignee_user_id,
        "is_taken": 1 if inst.is_taken else 0,
        "planned_start": _fmt_dt(inst.planned_start),
        "planned_end": _fmt_dt(inst.planned_end),
        "actual_start": _fmt_dt(inst.actual_start),
        "actual_end": _fmt_dt(inst.actual_end),
        "actual_minutes": inst.actual_minutes,
        "color": meta["color"],
        "important": meta["important"],
        "created_at": _fmt_dt(inst.created_at),
    }


def me_dict(actor: Actor) -> Dict[str, Any]:
    return {
        "id": actor.user_id, "role": role_key(actor.role_id), "roleId": actor.role_id,
        "name": actor.full_name, "firstName": actor.name, "lastName": actor.last_name,
        "organizationId": actor.org_id, "memberId": actor.member_id,
    }


def get_snapshot(session: Session, actor: Actor) -> Dict[str, Any]:
    materialize_regular(session, actor.org_id)

    members = org_members(session, actor.org_id)
    visible = set(visible_ids(actor, members))
    assignable = assignable_ids(actor, members)
    since = datetime.now() - timedelta(days=settings.SNAPSHOT_PAST_DAYS)

    rows = (
        session.query(TaskInstance, Task, ScheduleRule)
        .join(Task, Task.task_id == TaskInstance.task_id)
        .outerjoin(ScheduleRule, ScheduleRule.rule_id == Task.schedule_rule_id)
        .filter(
            Task.organization_id == actor.org_id, Task.is_active == True,  # noqa: E712
            or_(TaskInstance.planned_end.is_(None), TaskInstance.planned_end >= since),
        )
        .order_by(TaskInstance.planned_start, TaskInstance.instance_id)
        .all()
    )

    tasks_out: Dict[int, Dict[str, Any]] = {}
    instances_out: List[Dict[str, Any]] = []
    deadline_pairs: List[Tuple[TaskInstance, Task, Dict[str, Any]]] = []
    for inst, task, rule in rows:
        meta = _meta(task)
        if meta["kind"] == "deadline":
            deadline_pairs.append((inst, task, meta))
            continue
        uid = inst.assignee_user_id
        if uid is None:
            if not _is_pool(task):
                continue
        elif uid not in visible:
            continue
        if task.task_id not in tasks_out:
            tasks_out[task.task_id] = _task_dict(task, rule, meta)
        instances_out.append(_instance_dict(inst, task, meta))
        entry = tasks_out[task.task_id]
        if entry["type"] == "single" and entry["planned_end"] is None:      # для клиента: срок разовой задачи
            entry["planned_start"] = _fmt_dt(inst.planned_start)
            entry["planned_end"] = _fmt_dt(inst.planned_end)

    avail: Dict[int, List[int]] = {}
    dl_ids = [t.task_id for _, t, _ in deadline_pairs]
    if dl_ids:
        for tid, uid in session.query(TaskAvailability.task_id, TaskAvailability.user_id) \
                .filter(TaskAvailability.task_id.in_(dl_ids)).all():
            avail.setdefault(tid, []).append(uid)

    deadlines_out: List[Dict[str, Any]] = []
    for inst, task, meta in deadline_pairs:
        assigned = inst.assignee_user_id
        available = sorted(avail.get(task.task_id, [])) if assigned is None else []
        if assigned is not None:
            if assigned not in visible:
                continue
        elif not any(u in visible for u in available):
            continue
        deadlines_out.append({
            "id": task.task_id, "text": task.title, "deadlineAt": inst.planned_end.strftime("%Y-%m-%dT%H:%M"),
            "color": meta["color"], "important": meta["important"],
            "assignedUserId": assigned, "availableForIds": available or None,
            "completedAt": _fmt_dt(inst.actual_end),
        })
    deadlines_out.sort(key=lambda d: d["deadlineAt"])

    return {
        "me": me_dict(actor),
        "users": [{"id": m.user_id, "name": m.full_name, "role": role_key(m.role_id), "roleId": m.role_id}
                  for m in members],
        "visibleUserIds": sorted(visible),
        "assignableUserIds": assignable,
        "tasks": tasks_out,
        "instances": instances_out,
        "deadlines": deadlines_out,
        "serverTime": _fmt_dt(datetime.now()),
    }


# ============================================================
# СОЗДАНИЕ ЗАДАЧ
# ============================================================
def _insert_single(session: Session, org_id: int, user_id: Optional[int], title: str, weight: int,
                   estimate: int, meta_json: str, planned_start: Optional[datetime],
                   planned_end: datetime) -> Tuple[Task, TaskInstance]:
    rule = ScheduleRule(weekday_mask=0, time=planned_end.time().replace(microsecond=0),
                        start_date=planned_end.date(), end_date=planned_end.date(), interval=None)
    session.add(rule)
    session.flush()
    task = Task(organization_id=org_id, title=title, description=None, user_id=user_id,
                schedule_rule_id=rule.rule_id, deadline_offset=None, estimated_minutes=estimate,
                weight=weight, check_list_template=meta_json, is_active=True)
    session.add(task)
    session.flush()
    inst = TaskInstance(task_id=task.task_id, assignee_user_id=user_id, status_id=STATUS_NEW,
                        planned_start=planned_start, planned_end=planned_end, is_taken=user_id is not None)
    session.add(inst)
    session.flush()
    return task, inst


def create_task(session: Session, actor: Actor, p: Dict[str, Any]) -> Dict[str, Any]:
    _require_edit(actor)
    kind = p.get("type")
    if kind not in ("single", "regular"):
        raise _bad("Неизвестный тип задачи")

    title = _title(p.get("title"))
    weight = _parse_int(p.get("weight", 3), "weight", 1, 10)
    estimate = _parse_int(p.get("estimate", 30), "estimate", 1, 24 * 60)
    color = _color(p.get("color"))
    important = bool(p.get("important"))
    is_pool = bool(p.get("isPool"))
    meta_json = _meta_json("task", color, important)

    members = org_members(session, actor.org_id)
    names = {m.user_id: m.full_name for m in members}
    allowed = set(assignable_ids(actor, members))
    employee_ids = [] if is_pool else _int_list(p.get("employeeIds"), "employeeIds")
    if not is_pool:
        if not employee_ids:
            raise _bad("Выберите хотя бы одного сотрудника или поставьте флаг «В пул»")
        denied = [e for e in employee_ids if e not in allowed]
        if denied:
            raise ApiError("forbidden", "Нельзя назначать задачи выбранным сотрудникам", {"ids": denied})
    targets: List[Optional[int]] = [None] if is_pool else list(employee_ids)

    if kind == "single":
        return _create_single(session, actor, p, names, targets, title, weight, estimate, meta_json)
    return _create_regular(session, actor, p, names, targets, title, weight, estimate, meta_json)


def _create_single(session, actor, p, names, targets, title, weight, estimate, meta_json) -> Dict[str, Any]:
    day = _parse_date(p.get("date"), "date")
    end_t = _parse_time(p.get("endTime"), "endTime")
    start_t = _parse_time(p.get("startTime"), "startTime", required=False)
    if start_t is not None and end_t <= start_t:
        raise _bad("Время окончания должно быть позже начала")
    planned_end = datetime.combine(day, end_t)
    planned_start = datetime.combine(day, start_t) if start_t else None
    if planned_end <= datetime.now():
        raise _bad("Дедлайн должен быть в будущем")

    real_targets = [t for t in targets if t is not None]
    check = planned_start is not None and real_targets
    if check:                                                   # проверка №1 — до записи
        _collect_conflicts(session, real_targets, planned_start, planned_end, names)

    created = [_insert_single(session, actor.org_id, uid, title, weight, estimate, meta_json,
                              planned_start, planned_end) for uid in targets]

    if check:                                                   # проверка №2 — после записи, до commit
        own = [inst.instance_id for _, inst in created]
        _collect_conflicts(session, real_targets, planned_start, planned_end, names, exclude_ids=own)
    _notify(session, actor, real_targets,
            f"📌 Вам назначена задача «{title}»\n{_when_text(planned_start, planned_end)}")
    return {"created": [{"taskId": t.task_id, "instanceId": i.instance_id} for t, i in created],
            "warnings": []}


def _create_regular(session, actor, p, names, targets, title, weight, estimate, meta_json) -> Dict[str, Any]:
    days = _int_list(p.get("weekdays"), "weekdays")
    if not days or any(d > 6 for d in days):
        raise _bad("Выберите хотя бы один день недели")
    mask = _js_days_to_mask(days)
    deadline_t = _parse_time(p.get("deadlineTime"), "deadlineTime")
    start_t = _parse_time(p.get("startTime"), "startTime", required=False)
    if start_t is not None and start_t >= deadline_t:
        raise _bad("Время начала должно быть строго раньше времени дедлайна")
    period_start = _parse_date(p.get("periodStart"), "periodStart", required=False) or date.today()
    period_end = _parse_date(p.get("periodEnd"), "periodEnd", required=False)
    if period_end is not None and period_end < period_start:
        raise _bad("Дата окончания не может быть раньше даты начала")

    blocking = []
    for uid in targets:
        if uid is None:
            continue
        found = find_regular_rule_conflicts(session, actor.org_id, uid, mask, start_t, deadline_t,
                                            period_start, period_end)
        if found:
            blocking.append({"userId": uid, "userName": names.get(uid, str(uid)), "items": found})
    if blocking:
        lines = [f"Нельзя создать регулярную задачу «{title}»: она пересекается с другими регулярными задачами.",
                 "Исправьте расписание или исполнителя и попробуйте снова:", ""]
        for entry in blocking:
            lines.append(f"• {entry['userName']}:")
            for item in entry["items"]:
                lines.append(f"   — «{title}» пересекается с «{item['title']}» ({item['start']}–{item['end']})")
            lines.append("")
        raise ApiError("conflict", "\n".join(lines).strip(), blocking)

    offset = None
    rule_time = deadline_t
    if start_t is not None:
        rule_time = start_t
        offset = _minutes(deadline_t) - _minutes(start_t)

    tasks: List[Task] = []
    for uid in targets:
        rule = ScheduleRule(weekday_mask=mask, time=rule_time, start_date=period_start,
                            end_date=period_end, interval=None)
        session.add(rule)
        session.flush()
        task = Task(organization_id=actor.org_id, title=title, description=None, user_id=uid,
                    schedule_rule_id=rule.rule_id, deadline_offset=offset, estimated_minutes=estimate,
                    weight=weight, check_list_template=meta_json, is_active=True)
        session.add(task)
        session.flush()
        tasks.append(task)

    instances = materialize_regular(session, actor.org_id, [t.task_id for t in tasks])

    # Регулярная задача «главнее» разовых: пересечения с ними — только предупреждение.
    warnings: List[str] = []
    for inst in instances:
        if inst.assignee_user_id is None or inst.planned_start is None or len(warnings) >= 20:
            continue
        for other, other_task in _overlapping(session, inst.assignee_user_id, inst.planned_start,
                                              inst.planned_end, exclude_ids=[inst.instance_id],
                                              exclude_regular=True):
            warnings.append(
                f"{names.get(inst.assignee_user_id, inst.assignee_user_id)}, {inst.planned_start:%d.%m} "
                f"{_hm(inst.planned_start)}–{_hm(inst.planned_end)}: пересекается с «{other_task.title}» "
                f"({_hm(other.planned_start)}–{_hm(other.planned_end)})")
    days_text = ", ".join(("вс", "пн", "вт", "ср", "чт", "пт", "сб")[d] for d in sorted(days, key=lambda x: (x + 6) % 7))
    period = f"с {period_start:%d.%m.%Y}" + (f" по {period_end:%d.%m.%Y}" if period_end else " (бессрочно)")
    when_txt = f"{_hm(start_t)}–{_hm(deadline_t)}" if start_t else f"до {_hm(deadline_t)}"
    _notify(session, actor, [t for t in targets if t is not None],
            f"🔁 Вам назначена регулярная задача «{title}»\n{days_text}, {when_txt}, {period}")
    return {"created": [{"taskId": t.task_id} for t in tasks], "warnings": warnings}


# ============================================================
# ИЗМЕНЕНИЕ / УДАЛЕНИЕ / ПЕРЕНОС
# ============================================================
def update_task(session: Session, actor: Actor, p: Dict[str, Any]) -> Dict[str, Any]:
    inst, task, rule = _load_instance(session, actor, p.get("instanceId"))
    if _meta(task)["kind"] == "deadline":
        raise _bad("Дедлайны изменяются отдельно")
    _check_can_modify(session, actor, inst)
    if _is_regular(rule):
        raise _bad("Регулярную задачу изменить нельзя — только удалить весь ряд")

    meta = _meta(task)
    title = _title(p["title"]) if "title" in p else task.title
    weight = _parse_int(p["weight"], "weight", 1, 10) if "weight" in p else task.weight
    estimate = _parse_int(p["estimate"], "estimate", 1, 24 * 60) if "estimate" in p else task.estimated_minutes
    important = bool(p["important"]) if "important" in p else meta["important"]
    color = _color(p["color"]) if "color" in p else meta["color"]

    new_start, new_end = inst.planned_start, inst.planned_end
    if any(k in p for k in ("date", "startTime", "endTime")):
        base = inst.planned_end or inst.planned_start
        day = _parse_date(p["date"], "date") if p.get("date") else (base.date() if base else None)
        if day is None:
            raise _bad("Не указана дата")
        end_t = _parse_time(p.get("endTime"), "endTime", required=False) if "endTime" in p \
            else (inst.planned_end.time() if inst.planned_end else None)
        start_t = _parse_time(p.get("startTime"), "startTime", required=False) if "startTime" in p \
            else (inst.planned_start.time() if inst.planned_start else None)
        if end_t is None:
            raise _bad("Не указан дедлайн")
        if start_t is not None and end_t <= start_t:
            raise _bad("Время окончания должно быть позже начала")
        new_start = datetime.combine(day, start_t) if start_t else None
        new_end = datetime.combine(day, end_t)

    members = org_members(session, actor.org_id)
    names = {m.user_id: m.full_name for m in members}
    check = inst.assignee_user_id is not None and new_start is not None
    if check:                                                   # проверка №1
        _collect_conflicts(session, [inst.assignee_user_id], new_start, new_end, names, [inst.instance_id])

    task.title, task.weight, task.estimated_minutes = title, weight, estimate
    task.check_list_template = _meta_json("task", color, important)
    old_title, old_start, old_end = task.title, inst.planned_start, inst.planned_end
    inst.planned_start, inst.planned_end = new_start, new_end
    if rule is not None and new_end is not None:                # заглушка правила следует за сроком
        rule.time = new_end.time().replace(microsecond=0)
        rule.start_date = rule.end_date = new_end.date()
    session.flush()

    if check:                                                   # проверка №2
        _collect_conflicts(session, [inst.assignee_user_id], new_start, new_end, names, [inst.instance_id])
    if (old_start, old_end) != (new_start, new_end):
        _notify(session, actor, [inst.assignee_user_id],
                f"🔄 Задача «{title}» перенесена\nБыло: {_when_text(old_start, old_end)}\n"
                f"Стало: {_when_text(new_start, new_end)}")
    elif old_title != title:
        _notify(session, actor, [inst.assignee_user_id], f"✏️ Задача «{old_title}» переименована в «{title}»")
    return {"instanceId": inst.instance_id}


def _drop_task(session: Session, task: Task) -> None:
    rule_id = task.schedule_rule_id
    session.delete(task)                    # каскадом: экземпляры и Task_Availability
    session.flush()
    if rule_id is not None:
        still_used = session.query(Task.task_id).filter(Task.schedule_rule_id == rule_id).first()
        if not still_used:
            rule = session.get(ScheduleRule, rule_id)
            if rule is not None:
                session.delete(rule)
                session.flush()


def delete_task(session: Session, actor: Actor, p: Dict[str, Any]) -> Dict[str, Any]:
    inst, task, rule = _load_instance(session, actor, p.get("instanceId"))
    if _meta(task)["kind"] == "deadline":
        raise _bad("Дедлайны удаляются отдельно")
    _check_can_modify(session, actor, inst)
    title, s_at, e_at = task.title, inst.planned_start, inst.planned_end

    if _is_regular(rule):
        owners = {task.user_id, inst.assignee_user_id}
        _drop_task(session, task)           # регулярную удаляем целым рядом
        _notify(session, actor, owners, f"🗑 Регулярная задача «{title}» удалена (весь ряд)")
        return {"deletedTaskId": task.task_id, "series": True}

    assignee = inst.assignee_user_id
    session.delete(inst)
    session.flush()
    _notify(session, actor, [assignee], f"🗑 Задача «{title}» удалена\n{_when_text(s_at, e_at)}")
    remaining = session.query(TaskInstance.instance_id).filter(TaskInstance.task_id == task.task_id).first()
    if remaining is None:
        session.refresh(task)
        _drop_task(session, task)
    return {"deletedInstanceId": inst.instance_id, "series": False}


# ============================================================
# ПУЛ
# ============================================================
def pool_range() -> Tuple[datetime, datetime]:
    """Регулярные задачи из пула можно брать: с начала сегодняшнего дня до конца следующей недели."""
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    return (datetime.combine(today, time.min),
            datetime.combine(week_start + timedelta(days=14), time.min))


def take_pool_task(session: Session, actor: Actor, p: Dict[str, Any]) -> Dict[str, Any]:
    if not actor.is_employee:
        raise ApiError("forbidden", "Брать задачи из пула могут только сотрудники")
    inst, task, rule = _load_instance(session, actor, p.get("instanceId"))
    if _meta(task)["kind"] == "deadline" or not _is_pool(task):
        raise _bad("Это не задача из пула")
    if inst.assignee_user_id is not None or inst.actual_end is not None:
        raise ApiError("conflict", "Задачу уже взял другой сотрудник")

    if _is_regular(rule):
        when = inst.planned_start or inst.planned_end
        lo, hi = pool_range()
        if when is None or not (lo <= when < hi):
            raise _bad("Эту регулярную задачу пока нельзя взять: доступны текущая и следующая недели")

    if inst.planned_start and inst.planned_end:                 # проверка №1
        names = {actor.user_id: actor.full_name}
        try:
            _collect_conflicts(session, [actor.user_id], inst.planned_start, inst.planned_end,
                               names, [inst.instance_id])
        except ApiError as exc:
            raise ApiError("conflict", f"Нельзя взять задачу «{task.title}»:\n{exc.message}", exc.details)

    updated = (                                                 # проверка №2 — условный UPDATE
        session.query(TaskInstance)
        .filter(TaskInstance.instance_id == inst.instance_id, TaskInstance.assignee_user_id.is_(None))
        .update({TaskInstance.assignee_user_id: actor.user_id, TaskInstance.is_taken: True},
                synchronize_session=False)
    )
    if updated != 1:
        raise ApiError("conflict", "Задачу уже взял другой сотрудник")
    return {"instanceId": inst.instance_id}


def return_pool_task(session: Session, actor: Actor, p: Dict[str, Any]) -> Dict[str, Any]:
    inst, task, _ = _load_instance(session, actor, p.get("instanceId"))
    if not _is_pool(task) or inst.assignee_user_id != actor.user_id:
        raise ApiError("forbidden", "Отказаться можно только от своей задачи из пула")
    if inst.actual_end is not None:
        raise _bad("Задача уже завершена")
    updated = (
        session.query(TaskInstance)
        .filter(TaskInstance.instance_id == inst.instance_id, TaskInstance.assignee_user_id == actor.user_id)
        .update({TaskInstance.assignee_user_id: None, TaskInstance.is_taken: False}, synchronize_session=False)
    )
    if updated != 1:
        raise ApiError("conflict", "Состояние задачи изменилось, обновите страницу")
    return {"instanceId": inst.instance_id}


# ============================================================
# ДЕДЛАЙНЫ
# ============================================================
def _insert_deadline(session: Session, org_id: int, user_id: Optional[int], title: str, when: datetime,
                     meta_json: str, available: Sequence[int]) -> Task:
    rule = ScheduleRule(weekday_mask=0, time=when.time().replace(microsecond=0),
                        start_date=when.date(), end_date=when.date(), interval=None)
    session.add(rule)
    session.flush()
    task = Task(organization_id=org_id, title=title, description=None, user_id=user_id,
                schedule_rule_id=rule.rule_id, estimated_minutes=60, weight=1,
                check_list_template=meta_json, is_active=True)
    session.add(task)
    session.flush()
    session.add(TaskInstance(task_id=task.task_id, assignee_user_id=user_id, status_id=STATUS_NEW,
                             planned_start=None, planned_end=when, is_taken=user_id is not None))
    for uid in available:
        session.add(TaskAvailability(task_id=task.task_id, user_id=uid))
    session.flush()
    return task


def create_deadline(session: Session, actor: Actor, p: Dict[str, Any]) -> Dict[str, Any]:
    _require_edit(actor)
    title = _title(p.get("title"))
    when = _parse_datetime(p.get("deadlineAt"), "deadlineAt")
    color = _color(p.get("color"))
    meta_json = _meta_json("deadline", color, bool(p.get("important")))
    selective = bool(p.get("selective"))
    employee_ids = _int_list(p.get("employeeIds"), "employeeIds")
    if not employee_ids:
        raise _bad("Выберите хотя бы одного сотрудника")

    members = org_members(session, actor.org_id)
    denied = [e for e in employee_ids if e not in set(assignable_ids(actor, members))]
    if denied:
        raise ApiError("forbidden", "Нельзя назначить дедлайн выбранным сотрудникам", {"ids": denied})

    if selective:
        tasks = [_insert_deadline(session, actor.org_id, None, title, when, meta_json, employee_ids)]
    else:
        tasks = [_insert_deadline(session, actor.org_id, uid, title, when, meta_json, ()) for uid in employee_ids]
    _notify(session, actor, employee_ids,
            (f"⏰ Вам доступен дедлайн «{title}» (до {when:%d.%m.%Y %H:%M}). Его можно принять в мини-приложении"
             if selective else f"⏰ Вам назначен дедлайн «{title}»\nСрок: {when:%d.%m.%Y %H:%M}"))
    return {"created": [{"id": t.task_id} for t in tasks]}


def _load_deadline(session: Session, actor: Actor, task_id: Any) -> Tuple[Task, TaskInstance]:
    tid = _parse_int(task_id, "id", 1)
    task = session.get(Task, tid)
    if task is None or task.organization_id != actor.org_id or not task.is_active \
            or _meta(task)["kind"] != "deadline":
        raise ApiError("not_found", "Дедлайн не найден")
    inst = session.query(TaskInstance).filter(TaskInstance.task_id == tid).order_by(TaskInstance.instance_id).first()
    if inst is None:
        raise ApiError("not_found", "Дедлайн не найден")
    return task, inst


def accept_deadline(session: Session, actor: Actor, p: Dict[str, Any]) -> Dict[str, Any]:
    task, inst = _load_deadline(session, actor, p.get("id"))
    offered = session.query(TaskAvailability).filter(
        TaskAvailability.task_id == task.task_id, TaskAvailability.user_id == actor.user_id).first()
    if offered is None:
        raise ApiError("forbidden", "Этот дедлайн вам недоступен")            # проверка №1

    updated = (                                                                 # проверка №2
        session.query(TaskInstance)
        .filter(TaskInstance.instance_id == inst.instance_id, TaskInstance.assignee_user_id.is_(None))
        .update({TaskInstance.assignee_user_id: actor.user_id, TaskInstance.is_taken: True},
                synchronize_session=False)
    )
    if updated != 1:
        raise ApiError("conflict", "Дедлайн уже принял другой сотрудник")
    task.user_id = actor.user_id
    others = [a.user_id for a in session.query(TaskAvailability)
              .filter(TaskAvailability.task_id == task.task_id)]
    session.query(TaskAvailability).filter(TaskAvailability.task_id == task.task_id) \
        .delete(synchronize_session=False)
    notify.enqueue(session, others,
                   f"ℹ️ Дедлайн «{task.title}» принял {actor.full_name}", exclude=[actor.user_id])
    return {"id": task.task_id}


def delete_deadline(session: Session, actor: Actor, p: Dict[str, Any]) -> Dict[str, Any]:
    _require_edit(actor)
    task, inst = _load_deadline(session, actor, p.get("id"))
    allowed = set(assignable_ids(actor, org_members(session, actor.org_id)))
    if inst.assignee_user_id is not None:
        scope = {inst.assignee_user_id}
    else:
        scope = {a.user_id for a in session.query(TaskAvailability).filter(TaskAvailability.task_id == task.task_id)}
    if scope and not (scope & allowed):
        raise ApiError("forbidden", "Нельзя удалить дедлайн этого сотрудника")
    title, recipients = task.title, list(scope)
    _drop_task(session, task)
    _notify(session, actor, recipients, f"🗑 Дедлайн «{title}» удалён")
    return {"id": task.task_id}


# ============================================================
# ФОНОВОЕ ОБСЛУЖИВАНИЕ
# ============================================================
def materialize_all(session: Session) -> int:
    """Периодически: досоздать экземпляры регулярных задач для всех организаций."""
    total = 0
    for (org_id,) in session.query(Organization.organization_id).all():
        total += len(materialize_regular(session, org_id))
    return total
