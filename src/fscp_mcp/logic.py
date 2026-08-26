"""Рендер логики ГК в читаемый текст.

Логика хранится деревом <Logic>/<*ClausesGroup>/<Clauses>/<GKClause>, где сами
условия — это перечисления на английском и списки голых GUID'ов. В таком виде
её нельзя ни прочитать, ни обсудить, поэтому здесь она разворачивается в строки
вида «Если Пожар2 в любой из зон: 1.Склад, 2.Коридор».
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any

from .archive import guid_list, text

if TYPE_CHECKING:
    from .archive import FscpArchive

#: Группы условий внутри <Logic> и их назначение.
CLAUSE_GROUPS: dict[str, str] = {
    "OnClausesGroup": "Включение",
    "OffClausesGroup": "Выключение",
    "OnNowClausesGroup": "Включить немедленно",
    "OffNowClausesGroup": "Выключить немедленно",
    "StopClausesGroup": "Остановка",
}

STATE_TYPES: dict[str, str] = {
    "Fire1": "Пожар1",
    "Fire2": "Пожар2",
    "Attention": "Внимание",
    "Failure": "Неисправность",
    "On": "Включено",
    "Off": "Выключено",
    "TurningOn": "Включается",
    "TurningOff": "Выключается",
    "AutoOff": "Автоматика отключена",
    "Ignore": "Обход",
    "Test": "Тест",
    "Norm": "Норма",
}

OPERATIONS: dict[str, str] = {
    "AnyDevice": "на любом из устройств",
    "AllDevices": "на всех устройствах",
    "AnyZone": "в любой из зон",
    "AllZones": "во всех зонах",
    "AnyGuardZone": "в любой из охранных зон",
    "AllGuardZones": "во всех охранных зонах",
    "AnyDirection": "в любом из направлений",
    "AllDirections": "во всех направлениях",
    "AnyDelay": "в любом из сценариев",
    "AllDelays": "во всех сценариях",
    "AnyMPT": "в любом из МПТ",
    "AllMPTs": "во всех МПТ",
    "AnyDoor": "в любой из точек доступа",
    "AllDoors": "во всех точках доступа",
    "AnyPumpStation": "в любой из насосных станций",
    "AllPumpStations": "во всех насосных станциях",
}

JOIN = {"Or": "ИЛИ", "And": "И"}

#: Списки ссылок внутри GKClause.
UID_FIELDS = (
    "DeviceUIDs",
    "ZoneUIDs",
    "GuardZoneUIDs",
    "DirectionUIDs",
    "DelayUIDs",
    "DoorUIDs",
    "MPTUIDs",
    "PumpStationsUIDs",
)

MAX_LISTED = 12


def resolve(archive: FscpArchive, uid: str) -> str:
    """Имя объекта по GUID — устройства и объекты верхнего уровня вперемешку."""
    device = archive.devices_by_uid.get(uid)
    if device is not None:
        return device.name
    ref = archive.objects_by_uid.get(uid)
    if ref is not None:
        return ref.name
    plan = archive.plans_by_uid.get(uid)
    if plan is not None:
        return f"план {text(plan, 'Name')}"
    return uid


def render_clause(archive: FscpArchive, clause: ET.Element) -> str:
    condition = text(clause, "ClauseConditionType") or "If"
    state = text(clause, "StateType")
    operation = text(clause, "ClauseOperationType")

    targets: list[str] = []
    for tag in UID_FIELDS:
        targets.extend(resolve(archive, uid) for uid in guid_list(clause.find(tag)))

    head = "Если" if condition == "If" else condition
    parts = [head, STATE_TYPES.get(state, state) or "?"]
    if operation:
        parts.append(OPERATIONS.get(operation, operation))

    line = " ".join(p for p in parts if p)
    if not targets:
        return line
    shown = ", ".join(targets[:MAX_LISTED])
    if len(targets) > MAX_LISTED:
        shown += f", … ещё {len(targets) - MAX_LISTED}"
    return f"{line}: {shown}"


def render_group(archive: FscpArchive, group: ET.Element) -> dict[str, Any]:
    """Одна группа условий: свои GKClause плюс вложенные ClauseGroups."""
    clauses_container = group.find("Clauses")
    lines = [
        render_clause(archive, clause)
        for clause in (clauses_container.findall("GKClause") if clauses_container is not None else [])
    ]

    nested_container = group.find("ClauseGroups")
    nested = [
        render_group(archive, nested_group)
        for nested_group in (list(nested_container) if nested_container is not None else [])
    ]

    join = text(group, "ClauseJoinOperationType")
    payload: dict[str, Any] = {"join": JOIN.get(join, join), "clauses": lines}
    if nested:
        payload["groups"] = nested

    pim_uid = text(group, "PimUID").lower()
    if pim_uid and pim_uid != "00000000-0000-0000-0000-000000000000":
        payload["pim"] = resolve(archive, pim_uid)
    return payload


def flatten(payload: dict[str, Any]) -> str:
    """Схлопывает группу в одну строку, соединяя условия через И/ИЛИ."""
    join = f" {payload.get('join') or 'ИЛИ'} "
    parts = list(payload.get("clauses", []))
    parts.extend(f"({flatten(inner)})" for inner in payload.get("groups", []))
    return join.join(p for p in parts if p)


def render(archive: FscpArchive, owner: ET.Element, tag: str = "Logic") -> dict[str, Any]:
    """Разворачивает <Logic> объекта в {назначение группы: текст условия}."""
    container = owner.find(tag)
    if container is None:
        return {}

    result: dict[str, Any] = {}
    for group_tag, title in CLAUSE_GROUPS.items():
        group = container.find(group_tag)
        if group is None:
            continue
        payload = render_group(archive, group)
        line = flatten(payload)
        if line:
            result[title] = line
    return result


def render_direction_devices(archive: FscpArchive, owner: ET.Element) -> list[dict[str, str]]:
    """Устройства, которыми объект управляет, и подаваемая команда."""
    container = owner.find("DirectionDevices")
    if container is None:
        return []
    out = []
    for item in container.findall("DirectionDevice"):
        uid = text(item, "DeviceUid").lower()
        out.append({"device": resolve(archive, uid), "uid": uid, "command": text(item, "CommandType")})
    return out
