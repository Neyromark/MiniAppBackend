"""Демо-данные для пустой БД: та же организация, что и в demo-data.js старого фронтенда."""
import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from models import Member, Organization, OrganizationType, Role, User
from services import _insert_deadline, _insert_single, _meta_json
from models import TaskAvailability  # noqa: F401  (таблица создаётся через Base.metadata)

logger = logging.getLogger(__name__)

# (имя, фамилия, role_id) — MaxId = порядковый номер 1..5, как user.id в identifyUser.js
PEOPLE = [
    ("Иван", "Петров", 1),
    ("Анна", "Смирнова", 2),
    ("Сергей", "Иванов", 2),
    ("Елена", "Петрова", 3),
    ("Дмитрий", "Козлов", 3),
]


def seed_demo(session: Session) -> bool:
    if session.query(Member.member_id).first() is not None:
        return False

    # Роли создаются в порядке 1, 2, 3 (владелец, администратор, сотрудник)
    if session.query(Role.role_id).count() == 0:
        for name in ("Владелец", "Администратор", "Сотрудник"):
            session.add(Role(role_name=name))
        session.flush()
    otype = OrganizationType(org_type_name="Демо")
    session.add(otype)
    session.flush()
    org = Organization(org_name="Демо-организация", city="Москва", org_type_id=otype.org_type_id)
    session.add(org)
    session.flush()

    role_ids = {r.role_name: r.role_id for r in session.query(Role).all()}
    ordered = [role_ids["Владелец"], role_ids["Администратор"], role_ids["Сотрудник"]]
    uid = {}
    for idx, (first, last, role_index) in enumerate(PEOPLE, start=1):
        user = User(user_name=first, last_name=last, max_id=idx)
        session.add(user)
        session.flush()
        session.add(Member(organization_id=org.organization_id, role_id=ordered[role_index - 1],
                           user_id=user.user_id))
        uid[idx] = user.user_id
    session.flush()

    today = date.today()

    def at(offset_days: int, hhmm: str) -> datetime:
        h, m = map(int, hhmm.split(":"))
        return datetime.combine(today + timedelta(days=offset_days), time(h, m))

    events = [
        (1, "Стратегическая сессия", 1, "10:00", "12:00", "#F59E0B", True),
        (1, "Встреча с партнёрами", 0, "15:00", "16:30", "#7B9EFF", True),
        (2, "Созвон с командой", 0, "10:00", "11:00", "#7B9EFF", True),
        (2, "Подготовить отчёт", 1, "14:00", "16:00", "#C77DFF", False),
        (2, "Проверить макеты", -1, "09:30", "10:30", "#00BFFF", False),
        (2, "Обед с клиентом", 0, "13:00", "14:00", "#A78BFA", False),
        (2, "Демо для инвесторов", 2, "12:00", "13:30", "#22C55E", True),
        (3, "Деплой обновления", 0, "11:00", "12:30", "#EF4444", True),
        (3, "Код-ревью", 1, "15:00", "16:00", "#A78BFA", False),
        (3, "Архитектурная сессия", 3, "10:00", "11:30", "#00BFFF", False),
        (4, "Встреча с клиентом", 0, "09:00", "10:00", "#EC4899", True),
        (4, "Обновить документацию", -1, "16:00", "18:00", "#22C55E", False),
        (4, "Тренинг", 1, "10:00", "12:00", "#F59E0B", False),
        (5, "Анализ метрик", 0, "14:00", "15:30", "#7B9EFF", False),
        (5, "Планирование спринта", 1, "11:00", "12:00", "#00BFFF", True),
    ]
    for who, title, off, start, end, color, important in events:
        s, e = at(off, start), at(off, end)
        minutes = int((e - s).total_seconds() // 60)
        _insert_single(session, org.organization_id, uid[who], title, 8 if important else 4, minutes,
                       _meta_json("task", color, important), s, e)

    deadlines = [
        (1, "Стратегическая сессия с советом", at(5, "10:00"), "#F59E0B", []),
        (2, "Подготовить квартальный отчёт", at(2, "18:00"), "#7B9EFF", []),
        (3, "Обновить API-документацию", at(3, "12:30"), "#C77DFF", []),
        (None, "Разобрать тикеты поддержки", at(1, "17:00"), "#00BFFF", [2, 4, 5]),
        (4, "Согласовать макеты", at(-1, "14:00"), "#EF4444", []),
        (None, "Проверить релиз", at(0, "23:59"), "#22C55E", [2, 3, 4, 5]),
        (3, "Финальная вычитка пресс-релиза", at(-2, "10:00"), "#F59E0B", []),
    ]
    for who, title, when, color, available in deadlines:
        _insert_deadline(session, org.organization_id, uid[who] if who else None, title, when,
                         _meta_json("deadline", color, False), [uid[a] for a in available])

    logger.info("Загружены демо-данные (организация #%s)", org.organization_id)
    return True
