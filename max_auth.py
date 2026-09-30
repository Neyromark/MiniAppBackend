"""
Валидация WebAppData от MAX (https://dev.max.ru/docs/webapps/validation).

Алгоритм платформы:
  1. initData = строка «key1=value1&key2=value2&...&hash=...» (window.WebApp.initData
     или значение WebAppData из URL-фрагмента после «#»);
  2. каждый ключ — ровно один раз, hash — ровно один раз; hash сохраняем и исключаем;
  3. значения URL-декодируем, пары сортируем по ключу a→z, склеиваем через «\\n» → launch_params;
  4. secret_key = HMAC-SHA256(key="WebAppData", msg=BOT_TOKEN);
  5. подпись = hex(HMAC-SHA256(key=secret_key, msg=launch_params)); равна hash → данные подлинные.

Сверх спецификации (защита от повторного использования старых данных):
  * сравнение подписи — за постоянное время (hmac.compare_digest);
  * auth_date обязателен, не старше max_age секунд и не «из будущего» (допуск 60 с).
Пользователь берётся ТОЛЬКО из подписанного параметра user — не из initDataUnsafe.
"""
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, unquote

MAX_LENGTH = 8192          # initData реального клиента — сотни байт; больше — явный мусор
CLOCK_SKEW_SECONDS = 60


class AuthError(Exception):
    """Данные не прошли проверку. reason — для лога сервера; клиенту показываем общий текст."""

    def __init__(self, reason: str, expired: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.expired = expired


@dataclass(frozen=True)
class LaunchData:
    max_user_id: int
    auth_date: int
    params: Dict[str, str]
    user: Dict[str, Any]


def extract_web_app_data(value: str) -> str:
    """
    Принимает либо саму строку initData, либо URL / фрагмент с «WebAppData=…» (как в документации).
    Возвращает строку initData.
    """
    text = value.strip()
    if "#" in text:
        text = text.split("#", 1)[1]
    if text.startswith("WebAppData=") or "&WebAppData=" in text:
        pairs = [kv for kv in parse_qsl(text, keep_blank_values=True) if kv[0] == "WebAppData"]
        if len(pairs) != 1:
            raise AuthError("WebAppData должен встречаться ровно один раз")
        return pairs[0][1]                 # parse_qsl уже сделал первое URL-декодирование
    return text


def compute_hash(params: Dict[str, str], bot_token: str) -> str:
    """params — уже декодированные пары БЕЗ hash."""
    launch_params = "\n".join(f"{key}={params[key]}" for key in sorted(params))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(secret_key, launch_params.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_init_data(raw: Any, bot_token: str, max_age: int,
                       now: Optional[float] = None) -> LaunchData:
    if not bot_token:
        raise AuthError("BOT_TOKEN не настроен")
    if not isinstance(raw, str) or not raw.strip():
        raise AuthError("initData отсутствует")
    if len(raw) > MAX_LENGTH:
        raise AuthError("initData слишком длинный")

    data = extract_web_app_data(raw)

    params: Dict[str, str] = {}
    original_hash: Optional[str] = None
    for part in data.split("&"):
        if "=" not in part:
            raise AuthError("параметр без значения")
        key, value = part.split("=", 1)        # значение может содержать «=» — делим по первому
        if not key:
            raise AuthError("пустое имя параметра")
        if key in params or (key == "hash" and original_hash is not None):
            raise AuthError(f"параметр {key!r} повторяется")
        if key == "hash":
            original_hash = value
        else:
            params[key] = unquote(value)       # decodeURIComponent: «+» остаётся плюсом
    if original_hash is None:
        raise AuthError("hash отсутствует")

    expected = compute_hash(params, bot_token)
    if not hmac.compare_digest(expected.encode("ascii"), original_hash.strip().lower().encode("utf-8", "replace")):
        raise AuthError("подпись не совпадает")

    try:
        auth_date = int(params["auth_date"])
    except (KeyError, ValueError):
        raise AuthError("auth_date отсутствует или некорректен")
    current = time.time() if now is None else now
    if auth_date > current + CLOCK_SKEW_SECONDS:
        raise AuthError("auth_date из будущего")
    if max_age > 0 and current - auth_date > max_age:
        raise AuthError("данные запуска устарели", expired=True)

    try:
        user = json.loads(params.get("user", ""))
        max_user_id = int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise AuthError("в данных нет пользователя")
    if not isinstance(user, dict) or isinstance(user.get("id"), bool):
        raise AuthError("некорректный пользователь")
    return LaunchData(max_user_id, auth_date, params, user)
