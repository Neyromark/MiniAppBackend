"""Идентификация пользователя и иерархия ролей (аналог auth.py + visibility.py из Advanced)."""
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from errors import ApiError
from max_auth import AuthError, validate_init_data
from save_config import settings
from models import Member, ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_OWNER, User

logger = logging.getLogger(__name__)

_ROLE_KEYS = {ROLE_OWNER: "owner", ROLE_ADMIN: "admin", ROLE_EMPLOYEE: "employee"}
_ROLE_RANK = {ROLE_OWNER: 0, ROLE_ADMIN: 1, ROLE_EMPLOYEE: 2}


def role_key(role_id: Optional[int]) -> str:
    return _ROLE_KEYS.get(role_id, "employee")


def role_rank(role_id: Optional[int]) -> int:
    return _ROLE_RANK.get(role_id, 99)


@dataclass(frozen=True)
class Actor:
    user_id: int
    member_id: int
    org_id: int
    role_id: int
    name: Optional[str]
    last_name: Optional[str]

    @property
    def full_name(self) -> str:
        return f"{self.name or ''} {self.last_name or ''}".strip() or f"user#{self.user_id}"

    @property
    def rank(self) -> int:
        return role_rank(self.role_id)

    @property
    def can_edit(self) -> bool:
        """Создавать/менять задачи и дедлайны могут только владелец и администратор."""
        return self.role_id in (ROLE_OWNER, ROLE_ADMIN)

    @property
    def is_employee(self) -> bool:
        return self.role_id == ROLE_EMPLOYEE


def _to_actor(user: User, member: Member) -> Actor:
    return Actor(user.user_id, member.member_id, member.organization_id, member.role_id,
                 user.user_name, user.last_name)


def _base_query(session: Session):
    return (
        session.query(User, Member)
        .join(Member, Member.user_id == User.user_id)
        .filter(Member.organization_id.isnot(None), Member.role_id.isnot(None))
    )


class Lookup:
    """Результат поиска по MaxId: actor или причина отказа (для внятного сообщения и лога)."""
    NO_USER = "no_user"            # такого MaxId нет в таблице User (человек не проходил регистрацию в боте)
    NO_MEMBER = "no_member"        # User есть, но ни в одной организации не состоит
    INCOMPLETE = "incomplete"      # запись Member есть, но без организации или без роли
    AMBIGUOUS = "ambiguous"        # нарушено «один человек — одна организация» (или дубль MaxId)

    def __init__(self, actor: Optional[Actor] = None, reason: Optional[str] = None):
        self.actor, self.reason = actor, reason


_DENY_TEXT = {
    Lookup.NO_USER: "Вы не зарегистрированы. Откройте чат-бота и пройдите регистрацию, затем повторите вход",
    Lookup.NO_MEMBER: "Вы не состоите ни в одной организации. Вступите в организацию через чат-бота "
                      "или обратитесь к администратору",
    Lookup.INCOMPLETE: "Ваша учётная запись в организации настроена не полностью. Обратитесь к администратору",
    Lookup.AMBIGUOUS: "Ваша учётная запись найдена в нескольких организациях. "
                      "Обратитесь к администратору для исправления",
}


def lookup_by_max_id(session: Session, max_id: int) -> Lookup:
    users = session.query(User).filter(User.max_id == max_id).all()
    if not users:
        return Lookup(reason=Lookup.NO_USER)
    if len(users) > 1:                                   # два человека с одним MaxId — личность неоднозначна
        return Lookup(reason=Lookup.AMBIGUOUS)
    user = users[0]
    members = session.query(Member).filter(Member.user_id == user.user_id).order_by(Member.member_id).all()
    if not members:
        return Lookup(reason=Lookup.NO_MEMBER)
    complete = [m for m in members if m.organization_id is not None and m.role_id is not None]
    if not complete:
        return Lookup(reason=Lookup.INCOMPLETE)
    if len(complete) > 1:                                # «один человек — одна организация» нарушено данными
        return Lookup(reason=Lookup.AMBIGUOUS)
    return Lookup(actor=_to_actor(user, complete[0]))


def _find_by_max_id(session: Session, max_id: int) -> Optional[Actor]:
    return lookup_by_max_id(session, max_id).actor


def _dev_actor(session: Session, init_data: Any) -> Optional[Actor]:
    """
    ТОЛЬКО для разработки (DEV_AUTH=true): user.id берётся из неподписанного объекта.
    Если такого MaxId нет и включён DEV_FALLBACK_USER — первый администратор.
    """
    user = init_data.get("user") if isinstance(init_data, dict) else None
    try:
        max_id = int(user.get("id")) if isinstance(user, dict) else None
    except (TypeError, ValueError):
        max_id = None
    if max_id is not None:
        actor = _find_by_max_id(session, max_id)
        if actor:
            return actor
    if not settings.DEV_FALLBACK_USER:
        return None
    row = (_base_query(session).filter(Member.role_id == ROLE_ADMIN)
           .order_by(User.user_id, Member.member_id).first())
    if row is None:
        row = _base_query(session).order_by(User.user_id, Member.member_id).first()
    return _to_actor(*row) if row else None


def authenticate(session: Session, init_data: Any) -> Actor:
    """
    initData (строка WebAppData от MAX) → проверка подписи → пользователь по User.MaxId.
    Любая ошибка — ApiError("unauthorized"); подробности причины пишутся только в лог сервера.
    """
    if isinstance(init_data, str):
        try:
            launch = validate_init_data(init_data, settings.BOT_TOKEN or "", settings.AUTH_MAX_AGE_SECONDS)
        except AuthError as exc:
            logger.warning("Отклонён вход: %s", exc.reason)
            raise ApiError("unauthorized", "Сессия устарела. Закройте и откройте мини-приложение заново"
                           if exc.expired else "Не удалось подтвердить данные MAX. Откройте приложение из MAX",
                           {"reason": exc.reason})        # причина не секретна; нужна, чтобы понять, что именно не так
        found = lookup_by_max_id(session, launch.max_user_id)
        if found.actor is None:
            logger.warning("Вход отклонён: MaxId %s — %s", launch.max_user_id, found.reason)
            raise ApiError("unauthorized", _DENY_TEXT[found.reason])
        return found.actor

    if settings.DEV_AUTH and isinstance(init_data, dict):
        actor = _dev_actor(session, init_data)
        if actor is not None:
            return actor
        raise ApiError("unauthorized", "Пользователь не найден")

    kind = type(init_data).__name__
    hint = ("получен неподписанный объект: отладочный вход выключен (DEV_AUTH=false)"
            if isinstance(init_data, dict) else f"initData не строка WebAppData (тип {kind})")
    logger.warning("Отклонён вход: %s", hint)
    raise ApiError("unauthorized", "Не удалось подтвердить данные MAX. Откройте приложение из MAX",
                   {"reason": hint})


def load_actor(session: Session, user_id: int, member_id: int) -> Optional[Actor]:
    """Свежая загрузка актёра на КАЖДЫЙ запрос — смена роли/удаление применяются сразу."""
    row = _base_query(session).filter(User.user_id == user_id, Member.member_id == member_id).first()
    return _to_actor(*row) if row else None


def org_members(session: Session, org_id: int) -> List[Actor]:
    rows = (_base_query(session).filter(Member.organization_id == org_id)
            .order_by(Member.role_id, User.user_id).all())
    seen, result = set(), []
    for user, member in rows:
        if user.user_id in seen:
            continue
        seen.add(user.user_id)
        result.append(_to_actor(user, member))
    return result


def visible_ids(actor: Actor, members: List[Actor]) -> List[int]:
    """Себя + строго ниже по рангу."""
    return [m.user_id for m in members if m.user_id == actor.user_id or m.rank > actor.rank]


def assignable_ids(actor: Actor, members: List[Actor]) -> List[int]:
    """Владелец — всем (включая себя); админ — строго ниже; сотрудник — никому."""
    if actor.role_id == ROLE_OWNER:
        return [m.user_id for m in members]
    if actor.role_id == ROLE_ADMIN:
        return [m.user_id for m in members if m.rank > actor.rank]
    return []
