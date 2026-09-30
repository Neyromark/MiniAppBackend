"""
Настройки бэкенда (аналог CurrentChatBot/save_config.py). Секреты берутся из окружения / файла .env.

Почему пароль с «@» (и «:», «/», «#», «;») здесь безопасен:
пароль не вставляется в URL вида mssql+pyodbc://user:pass@host — там первый «@» ломает разбор.
Вместо этого собирается ODBC-строка, где значение PWD заключено в фигурные скобки {...}
(«}» внутри пароля удваивается), а в SQLAlchemy она передаётся через odbc_connect с URL-кодированием.
В .env пароль пишите как есть, без кавычек и экранирования:  DB_PASSWORD=Пример@Пароль1
"""
import os
from pathlib import Path
from urllib.parse import quote_plus

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")          # partition: «=» и «@» в значении не мешают
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env_file(BASE_DIR / ".env")


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _odbc_value(value: str) -> str:
    """Значение ODBC-атрибута в фигурных скобках; «}» экранируется удвоением."""
    return "{" + value.replace("}", "}}") + "}"


class settings:
    # --- сервер ---
    HOST = os.getenv("HOST", "127.0.0.1")           # 0.0.0.0 — чтобы открыть с телефона
    PORT = int(os.getenv("PORT", "8000"))

    # --- MSSQL (та же база, что у CurrentChatBot). Включается, если задан DB_HOST ---
    DB_HOST = os.getenv("DB_HOST") or None          # например 127.0.0.1
    DB_PORT = os.getenv("DB_PORT", "1433")
    DB_NAME = os.getenv("DB_NAME", "MaxBotDispecherTasks")
    DB_USER = os.getenv("DB_USER", "sa")
    DB_PASSWORD = os.getenv("DB_PASSWORD")          # секрет: только из окружения/.env
    DB_DRIVER = os.getenv("DB_DRIVER", "ODBC Driver 17 for SQL Server")

    # Полный override, если нужен (пароль с «@» тогда надо кодировать самому — лучше не пользоваться)
    DATABASE_URL = os.getenv("DATABASE_URL") or None
    SQLITE_PATH = os.getenv("SQLITE_PATH", str(BASE_DIR / "miniapp.db"))
    SQL_ECHO = _flag("SQL_ECHO", False)

    # --- поведение ---
    SEED_DEMO = _flag("SEED_DEMO", True)            # для общей БД с ботом поставьте false
    # --- Авторизация через WebAppData от MAX (https://dev.max.ru/docs/webapps/validation) ---
    # Токен бота, которым подписаны данные запуска. ОБЯЗАТЕЛЕН (секрет: только из окружения/.env).
    BOT_TOKEN = os.getenv("BOT_TOKEN") or None
    # Насколько старым может быть auth_date (сек). 0 — не проверять. По умолчанию 24 часа.
    AUTH_MAX_AGE_SECONDS = int(os.getenv("AUTH_MAX_AGE_SECONDS", "86400"))
    # ТОЛЬКО РАЗРАБОТКА: принимать неподписанный user.id (?user=N). В проде должно быть false.
    DEV_AUTH = _flag("DEV_AUTH", False)
    # Только вместе с DEV_AUTH: неизвестный MaxId → первый администратор.
    DEV_FALLBACK_USER = _flag("DEV_FALLBACK_USER", False)
    REGULAR_HORIZON_DAYS = int(os.getenv("REGULAR_HORIZON_DAYS", "60"))
    SNAPSHOT_PAST_DAYS = int(os.getenv("SNAPSHOT_PAST_DAYS", "60"))

    # Как часто искать изменения, сделанные ботом напрямую в БД (секунды)
    # (запасной механизм: основной канал — WebSocket /ws/bot, см. bot_link.py)
    WATCH_INTERVAL_SECONDS = float(os.getenv("WATCH_INTERVAL_SECONDS", "10"))

    # Общий секрет с CurrentChatBot для канала /ws/bot. Без него канал открыт только для localhost.
    BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN") or None

    @classmethod
    def notify_enabled(cls) -> bool:
        """Писать ли уведомления боту. По умолчанию — если задан BRIDGE_TOKEN или БД — MSSQL."""
        raw = os.getenv("BOT_NOTIFY")
        if raw not in (None, ""):
            return _flag("BOT_NOTIFY", False)
        return bool(cls.BRIDGE_TOKEN) or cls.database_url().startswith("mssql")

    @classmethod
    def check_auth_config(cls) -> None:
        """Fail-fast при старте: без BOT_TOKEN подпись проверить нечем, а DEV_AUTH без нужды — дыра."""
        if not cls.DEV_AUTH and not cls.BOT_TOKEN:
            raise RuntimeError(
                "Не задан BOT_TOKEN: без него нельзя проверить подпись WebAppData от MAX. "
                "Укажите BOT_TOKEN в .env (токен того же бота, что открывает мини-приложение).")

    @classmethod
    def uses_mssql(cls) -> bool:
        return not cls.DATABASE_URL and bool(cls.DB_HOST)

    @classmethod
    def odbc_connection_string(cls) -> str:
        if not cls.DB_HOST:
            raise RuntimeError("DB_HOST не задан")
        if cls.DB_PASSWORD is None:
            raise RuntimeError("DB_PASSWORD не задан (укажите в .env или переменной окружения)")
        return (
            f"DRIVER={_odbc_value(cls.DB_DRIVER)};"
            f"SERVER={cls.DB_HOST},{cls.DB_PORT};"
            f"DATABASE={_odbc_value(cls.DB_NAME)};"
            f"UID={_odbc_value(cls.DB_USER)};"
            f"PWD={_odbc_value(cls.DB_PASSWORD)};"
            "Encrypt=no;TrustServerCertificate=yes;APP=MiniAppBackend;"
        )

    @classmethod
    def database_url(cls) -> str:
        if cls.DATABASE_URL:
            return cls.DATABASE_URL
        if cls.DB_HOST:
            return "mssql+pyodbc:///?odbc_connect=" + quote_plus(cls.odbc_connection_string())
        return f"sqlite:///{cls.SQLITE_PATH}"
