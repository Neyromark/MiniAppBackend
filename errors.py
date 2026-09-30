from typing import Any, Optional


class ApiError(Exception):
    """Ожидаемая ошибка: уходит клиенту как {ok: false, error, message, details}."""

    def __init__(self, code: str, message: str, details: Optional[Any] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
