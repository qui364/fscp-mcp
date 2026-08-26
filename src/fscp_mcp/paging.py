"""Страничная выдача.

Ни один инструмент сервера не отдаёт коллекцию целиком: в реальных
конфигурациях это 5868 устройств, которые не помещаются в контекст модели.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def page(
    items: Sequence[Any],
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    key: str = "items",
) -> dict[str, Any]:
    """Возвращает срез с метаданными о полном размере коллекции."""
    total = len(items)
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    window = list(items[offset : offset + limit])
    result: dict[str, Any] = {
        "total": total,
        "offset": offset,
        "returned": len(window),
        key: window,
    }
    if offset + len(window) < total:
        result["next_offset"] = offset + len(window)
        result["hint"] = (
            f"показано {len(window)} из {total}; "
            f"следующая страница: offset={offset + len(window)}"
        )
    return result


def clip(text: str, max_chars: int) -> tuple[str, bool]:
    """Обрезает текст, сообщая, было ли усечение."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True
