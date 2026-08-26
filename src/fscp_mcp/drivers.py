"""Справочник драйверов Рубеж.

Таблица снята с RubezhDrivers.cs из поставки Рубеж и дальше ведётся прямо
в drivers.json: исходника .cs в репозитории больше нет.
В конфигурации у устройства нет ни имени, ни типа — только DriverUID,
поэтому без этой таблицы дерево устройств нечитаемо.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path

_TABLE = Path(__file__).with_name("drivers.json")

# Драйверы, задающие уровни адресации.
GK = "c052395d-043f-4590-a0b8-bc49867adc6a"        # Групповой контроллер
RSGK = "7aa244a1-bf4c-4b4b-85c7-d9e53df3071a"      # Резервная система ГК
LOCAL_NET = "938947c5-4624-4a1a-939c-60aeebf7b65c"  # Локальная сеть (корень)
KAU = "57c45124-9300-49bc-a268-68f3d929927b"       # Контроллер адресных устройств


@dataclass(frozen=True, slots=True)
class Driver:
    uid: str
    short: str
    description: str
    category: str
    no_address: bool = False
    is_group: bool = False
    flag5: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "uid": self.uid,
            "short": self.short,
            "description": self.description,
            "category": self.category,
            "no_address": self.no_address,
            "is_group": self.is_group,
        }


UNKNOWN = Driver(
    uid="",
    short="Unknown",
    description="Неизвестный тип устройства",
    category="",
)


@cache
def table() -> dict[str, Driver]:
    raw = json.loads(_TABLE.read_text(encoding="utf-8"))
    return {d["uid"]: Driver(**d) for d in raw}


def get(uid: str | None) -> Driver:
    if not uid:
        return UNKNOWN
    return table().get(uid.lower(), UNKNOWN)


def find(query: str) -> list[Driver]:
    """Поиск по подстроке в коротком имени, описании или категории."""
    needle = query.casefold().strip()
    if not needle:
        return sorted(table().values(), key=lambda d: (d.category, d.short))
    hits = [
        d
        for d in table().values()
        if needle in d.short.casefold()
        or needle in d.description.casefold()
        or needle in d.category.casefold()
    ]
    return sorted(hits, key=lambda d: (d.category, d.short))
