# CurrentMiniAppBackend — FastAPI + WebSocket (/ws, /ws/bot) + SQLAlchemy.
#
# Dockerfile намеренно не привязан к архитектуре: платформа задаётся при сборке.
# `docker build` НЕ читает docker-compose.yml и без --platform собирает под архитектуру
# демона (на Mac с Apple Silicon — linux/arm64 → на amd64-сервере «exec format error»).
#
# Сборка для сервера (linux/amd64, с драйвером MSSQL):
#   docker build --platform linux/amd64 --build-arg INSTALL_MSSQL=true -t current-miniapp-backend:latest .
# или то же самое через compose (платформа уже указана в docker-compose.yml):
#   INSTALL_MSSQL=true docker compose build
#
# Без INSTALL_MSSQL=true драйвера ODBC в образе нет — только SQLite в томе /data.

# bookworm (Debian 12) закреплён явно: тег 3.13-slim уже указывает на trixie, для которого
# репозиторий Microsoft подписан другим ключом, и сборка ломалась на apt-get update.
FROM python:3.13-slim-bookworm

ARG INSTALL_MSSQL=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    SQLITE_PATH=/data/miniapp.db

# Microsoft ODBC Driver 17 for SQL Server — только если нужен MSSQL (только amd64: для arm64 версии 17 нет).
# Порядок установки — по инструкции Microsoft, адаптированной под Docker:
#   - без sudo: сборка и так идёт от root;
#   - образ основан на Debian 12 (bookworm), а не на Ubuntu, поэтому репозиторий берётся из
#     config/debian/<версия>; версия читается из /etc/os-release (lsb_release в slim-образе нет);
#   - ключ кладётся в /usr/share/keyrings/microsoft-prod.gpg: prod.list для Debian ссылается на него
#     через signed-by, и ключ из trusted.gpg.d игнорируется («NO_PUBKEY EB3E94ADBE1229CF»).
RUN if [ "$INSTALL_MSSQL" = "true" ]; then \
        apt-get update \
        && apt-get install -y curl apt-transport-https gnupg2 \
        && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/microsoft-prod.gpg \
        && curl -fsSL "https://packages.microsoft.com/config/debian/$(. /etc/os-release && echo "$VERSION_ID")/prod.list" > /etc/apt/sources.list.d/mssql-release.list \
        && apt-get update \
        && ACCEPT_EULA=Y apt-get install -y msodbcsql17 unixodbc-dev \
        && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY *.py ./
# .env вшивается в образ (save_config.py читает /app/.env). Переменные из environment/--env-file/-e
# имеют приоритет над файлом. ВНИМАНИЕ: секреты видны каждому, у кого есть образ/архив .tar.
COPY .env ./

# Непривилегированный пользователь; /data — том для SQLite
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /data && chown app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)" || exit 1

CMD ["python", "app.py"]
