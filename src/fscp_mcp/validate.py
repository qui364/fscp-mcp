"""Проверки целостности конфигурации.

Разделены на две категории. **Ошибки** блокируют сохранение: это то, обо что
Global Monitor может споткнуться молча - висячий GUID, дубль UID, поля не в
том порядке. .NET XmlSerializer на неожиданном порядке обычно не падает, он
**пропускает поле**: файл откроется, настройка потеряется, и никто не заметит.

**Предупреждения** сохранение не блокируют: устаревшие подписи на планах и
сироты в Content/ встречаются и в рабочих конфигурациях, сделанных самой
программой, и объявлять их ошибками значило бы запретить сохранять то, что
Global Monitor сохраняет сам.

Все правила уникальности сняты с рабочих конфигураций, а не выведены из общих
соображений. В частности, IntAddress среди братьев уникальным **не является**:
у БМП рядом стоят «Линия БМП», БМПК и БМПП с одним адресом. Уникальна пара
(DriverUID, IntAddress) - 0 нарушений на 8 конфигурациях против 42 у наивного
правила.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import drivers, schema
from .archive import NIL_UID, text

if TYPE_CHECKING:
    from .archive import FscpArchive

#: Элементы, для которых известен канонический порядок полей.
ORDERED: dict[str, tuple[str, ...]] = {
    "GKDevice": schema.DEVICE_FIELDS,
    "GKZone": schema.ZONE_FIELDS,
    "GKDelay": schema.SCENARIO_FIELDS,
    "GKClause": schema.CLAUSE_FIELDS,
    "Plan": schema.PLAN_FIELDS,
    "PointObject": schema.POINT_FIELDS,
    "RectangleObject": schema.RECTANGLE_FIELDS,
}

#: Обязательные поля: без них объект нечитаем.
REQUIRED: dict[str, tuple[str, ...]] = {
    "GKDevice": ("UID", "DriverUID", "IntAddress", "Children"),
    "GKZone": ("UID", "No", "Name"),
    "GKDelay": ("UID", "No", "Name"),
    "GKClause": ("ClauseConditionType", "StateType", "ClauseOperationType"),
}


@dataclass(frozen=True, slots=True)
class Problem:
    level: str  # error | warning
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _is_subsequence(tags: list[str], canon: tuple[str, ...]) -> bool:
    stream = iter(canon)
    return all(tag in stream for tag in tags)


def duplicate_uids(archive: FscpArchive) -> list[Problem]:
    """Дубли UID. Индексы схлопывают их словарём и молчат."""
    found: list[Problem] = []
    for entry, root in (("GKDeviceConfiguration.xml", archive.gk),
                        ("PlansConfiguration.xml", archive.plans)):
        if root is None:
            continue
        seen: dict[str, int] = {}
        for element in root.iter():
            uid = text(element, "UID").lower()
            if not uid or uid == NIL_UID:
                continue
            seen[uid] = seen.get(uid, 0) + 1
        for uid, count in seen.items():
            if count > 1:
                found.append(
                    Problem(
                        "error",
                        "duplicate_uid",
                        f"{entry}: UID {uid} встречается {count} раз",
                    )
                )
    return found


def dangling_refs(archive: FscpArchive) -> list[Problem]:
    """Ссылки на объекты, которых в конфигурации нет."""
    known = (
        set(archive.devices_by_uid)
        | set(archive.objects_by_uid)
        | set(archive.plans_by_uid)
        | set(archive.images)
        | set(drivers.table())
    )
    # Объекты на планах адресуются своим UID из PlanElementUIDs объекта ГК.
    for placements in archive.plan_objects_by_plan.values():
        for _, node in placements:
            known.add(text(node, "UID").lower())

    found: list[Problem] = []
    for uid, refs in archive.refs_to.items():
        if uid in known:
            continue
        tags = ", ".join(sorted({r.tag for r in refs}))
        found.append(
            Problem(
                "warning",
                "dangling_ref",
                f"на несуществующий {uid} ссылаются {len(refs)} раз ({tags})",
            )
        )
    return found


def address_conflicts(archive: FscpArchive) -> list[Problem]:
    """Дубль пары (DriverUID, IntAddress) среди братьев.

    no_address-драйверы исключаются: они своего адреса не имеют и законно
    делят его с соседями.
    """
    found: list[Problem] = []
    for device in archive.devices_by_uid.values():
        seen: dict[tuple[str, int], int] = {}
        for child in device.children:
            if child.driver.no_address:
                continue
            key = (child.driver_uid, child.int_address)
            seen[key] = seen.get(key, 0) + 1
        for (driver_uid, address), count in seen.items():
            if count > 1:
                found.append(
                    Problem(
                        "error",
                        "address_conflict",
                        f"на {device.name} {count} устройств "
                        f"{drivers.get(driver_uid).short} с IntAddress={address}",
                    )
                )
    return found


def field_order(archive: FscpArchive) -> list[Problem]:
    """Порядок полей: XmlSerializer на чужом порядке молча теряет поле."""
    found: list[Problem] = []
    for root in (archive.gk, archive.plans):
        if root is None:
            continue
        for element in root.iter():
            canon = ORDERED.get(element.tag)
            if canon is None:
                continue
            tags = [c.tag for c in element]
            if not _is_subsequence(tags, canon):
                uid = text(element, "UID") or "?"
                lost = [t for t in tags if t not in canon]
                found.append(
                    Problem(
                        "error",
                        "field_order",
                        f"{element.tag} {uid}: поля не в каноническом порядке"
                        + (f", неизвестные: {', '.join(lost)}" if lost else ""),
                    )
                )
    return found


def required_fields(archive: FscpArchive) -> list[Problem]:
    found: list[Problem] = []
    for root in (archive.gk, archive.plans):
        if root is None:
            continue
        for element in root.iter():
            needed = REQUIRED.get(element.tag)
            if needed is None:
                continue
            missing = [tag for tag in needed if element.find(tag) is None]
            if missing:
                uid = text(element, "UID") or "?"
                found.append(
                    Problem(
                        "error",
                        "missing_field",
                        f"{element.tag} {uid}: нет полей {', '.join(missing)}",
                    )
                )
    return found


def empty_clauses(archive: FscpArchive) -> list[Problem]:
    """Условие без единой цели. В рабочих конфигурациях таких нет."""
    from . import logic

    found: list[Problem] = []
    for clause in archive.gk.iter("GKClause"):
        if any(_children(clause, field) for field in logic.UID_FIELDS):
            continue
        found.append(
            Problem(
                "error",
                "empty_clause",
                f"условие {text(clause, 'StateType')} без целей - "
                "Global Monitor такое прочитать не сможет",
            )
        )
    return found


def _children(parent: ET.Element, tag: str) -> int:
    found = parent.find(tag)
    return 0 if found is None else len(found)


def unknown_drivers(archive: FscpArchive) -> list[Problem]:
    found: list[Problem] = []
    for device in archive.devices_by_uid.values():
        if device.driver_uid and device.driver_uid not in drivers.table():
            found.append(
                Problem(
                    "warning",
                    "unknown_driver",
                    f"{device.address or device.uid}: драйвера "
                    f"{device.driver_uid} нет в справочнике",
                )
            )
    return found


def one_sided_links(archive: FscpArchive) -> list[Problem]:
    """Связь план-объект односторонняя.

    У объекта плана ItemUID смотрит на объект ГК, а у объекта ГК
    PlanElementUIDs держит UID объекта плана. Половина связи - висячий GUID.
    """
    found: list[Problem] = []
    for uid, placements in archive.plan_objects_by_item.items():
        owner = archive.devices_by_uid.get(uid) or archive.objects_by_uid.get(uid)
        if owner is None:
            continue
        container = owner.element.find("PlanElementUIDs")
        declared = (
            set()
            if container is None
            else {(g.text or "").strip().lower() for g in container.findall("guid")}
        )
        for _, node in placements:
            placement_uid = text(node, "UID").lower()
            if placement_uid and placement_uid not in declared:
                found.append(
                    Problem(
                        "warning",
                        "one_sided_link",
                        f"{owner.name}: объект плана {placement_uid} ссылается на "
                        "него, а обратной ссылки в PlanElementUIDs нет",
                    )
                )
    return found


#: Все проверки. Блокирующей считается не сама проверка, а уровень находки:
#: часть правил даёт и ошибки, и предупреждения.
CHECKS = (
    duplicate_uids,
    dangling_refs,
    address_conflicts,
    field_order,
    required_fields,
    empty_clauses,
    unknown_drivers,
    one_sided_links,
)


def preflight(archive: FscpArchive) -> list[Problem]:
    """Только то, что должно остановить запись.

    Блокирующими сделаны ровно те правила, которые выполняются на 100 %
    рабочих конфигураций: дубли UID, конфликт (DriverUID, IntAddress), порядок
    полей, обязательные поля, условие без целей. Висячие ссылки и незнакомые
    драйверы в блокирующие не годятся - они встречаются в файлах, записанных
    самим Global Monitor (367 и 108 случаев в двух рабочих конфигурациях), и
    отказ по ним запретил бы сохранять то, что программа сохраняет сама.
    """
    return [p for p in _all(archive) if p.level == "error"]


def _all(archive: FscpArchive) -> list[Problem]:
    found: list[Problem] = []
    for check in CHECKS:
        found.extend(check(archive))
    return found


def inspect(archive: FscpArchive) -> dict[str, Any]:
    """Всё сразу: и блокирующее, и предупреждения."""
    problems = _all(archive)
    errors = [p for p in problems if p.level == "error"]
    warnings = [p for p in problems if p.level == "warning"]
    return {
        "errors": [p.as_dict() for p in errors],
        "warnings": [p.as_dict() for p in warnings],
    }
