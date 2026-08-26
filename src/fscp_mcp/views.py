"""Сборка полезной нагрузки для инструментов MCP.

Отдельный слой от archive.py: там индексы и модель, здесь — превращение узлов
XML в компактные словари, которые не разорвут контекст модели. Планом
предполагались отдельные devices/zones/plans-модули, но builder'ы получились
однотипными и короткими, поэтому лежат вместе.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from . import logic
from .archive import Device, FscpArchive, ObjectRef, guid_list, text

#: Потолок узлов в текстовом дереве — оно печатается целиком, без страниц.
MAX_TREE_NODES = 300


def properties(element: ET.Element, tag: str = "Properties") -> dict[str, str]:
    container = element.find(tag)
    if container is None:
        return {}
    out: dict[str, str] = {}
    for prop in container.findall("GKProperty"):
        name = text(prop, "Name")
        if not name:
            continue
        string_value = text(prop, "StringValue")
        out[name] = string_value or text(prop, "Value")
    return out


def named_uids(archive: FscpArchive, element: ET.Element, tag: str) -> list[dict[str, str]]:
    """Список ссылок с разрешёнными именами."""
    return [
        {"uid": uid, "name": logic.resolve(archive, uid)}
        for uid in guid_list(element.find(tag))
    ]


def plan_placements(archive: FscpArchive, uid: str) -> list[dict[str, str]]:
    """Где объект нарисован на планах."""
    out = []
    for plan_uid, node in archive.plan_objects_by_item.get(uid, []):
        plan = archive.plans_by_uid.get(plan_uid)
        out.append(
            {
                "plan_uid": plan_uid,
                "plan": text(plan, "Name") if plan is not None else "",
                "element": node.tag,
                # Подпись кэшируется при отрисовке и после перенумерации АЛС
                # расходится с фактическим адресом — это штатный дефект.
                "label": text(node, "Name"),
            }
        )
    return out


def device_detail(archive: FscpArchive, device: Device) -> dict[str, Any]:
    element = device.element
    driver = device.driver

    payload: dict[str, Any] = {
        "uid": device.uid,
        "name": device.name,
        "address": device.address,
        "int_address": device.int_address,
        "driver": {
            "short": driver.short,
            "description": driver.description,
            "category": driver.category,
            "uid": device.driver_uid,
        },
        "is_disabled": text(element, "IsDisabled") == "true",
        "serial_no": text(element, "SerialNo"),
    }
    if device.description:
        payload["description"] = device.description
    if device.predefined_name:
        payload["predefined_name"] = device.predefined_name

    if device.parent is not None:
        payload["parent"] = {
            "uid": device.parent.uid,
            "name": device.parent.name,
            "address": device.parent.address,
        }
    payload["path"] = " / ".join(reversed([d.name for d in ancestry(device)]))

    props = properties(element)
    if props:
        payload["properties"] = props

    zones = named_uids(archive, element, "ZoneUIDs")
    if zones:
        payload["zones"] = zones
    guard_zones = named_uids(archive, element, "GuardZoneUIDs")
    if guard_zones:
        payload["guard_zones"] = guard_zones

    rendered = logic.render(archive, element)
    if rendered:
        payload["logic"] = rendered
    ns_logic = logic.render(archive, element, "NSLogic")
    if ns_logic:
        payload["ns_logic"] = ns_logic

    placements = plan_placements(archive, device.uid)
    if placements:
        payload["on_plans"] = placements

    if device.children:
        payload["children"] = [child.brief() for child in device.children]
    return payload


def ancestry(device: Device) -> list[Device]:
    chain = []
    current: Device | None = device
    while current is not None:
        chain.append(current)
        current = current.parent
    return chain


def object_detail(archive: FscpArchive, ref: ObjectRef) -> dict[str, Any]:
    element = ref.element
    payload: dict[str, Any] = {
        "uid": ref.uid,
        "kind": ref.kind,
        "no": ref.no,
        "name": ref.name,
    }

    for tag in ("Description", "DelayTime", "Hold", "DelayRegime",
                "Fire1Count", "Fire2Count", "IsFireB", "FireBDelayTime"):
        value = text(element, tag)
        if value:
            payload[tag.lower()] = value

    rendered = logic.render(archive, element)
    if rendered:
        payload["logic"] = rendered

    commanded = logic.render_direction_devices(archive, element)
    if commanded:
        payload["direction_devices"] = commanded

    if ref.kind in ("zone", "guard_zone"):
        payload["devices"] = [
            {"uid": d.uid, "name": d.name, "address": d.address}
            for d in devices_in_zone(archive, ref.uid)
        ]

    placements = plan_placements(archive, ref.uid)
    if placements:
        payload["on_plans"] = placements
    return payload


def devices_in_zone(archive: FscpArchive, zone_uid: str) -> list[Device]:
    """Устройства, привязанные к зоне (обратный индекс строится при открытии)."""
    return archive.devices_by_zone.get(zone_uid, [])


def plan_detail(archive: FscpArchive, plan_uid: str) -> dict[str, Any]:
    plan = archive.plans_by_uid[plan_uid]
    background = text(plan, "BackgroundImageSource")
    payload: dict[str, Any] = {
        "uid": plan_uid,
        "name": text(plan, "Name"),
        "no": text(plan, "No"),
        "width": text(plan, "Width"),
        "height": text(plan, "Height"),
        "parent_uid": archive.plan_parent.get(plan_uid),
        "children": [
            {"uid": uid, "name": text(archive.plans_by_uid[uid], "Name")}
            for uid in archive.plan_children.get(plan_uid, [])
        ],
    }
    if background:
        payload["background"] = image_reference(archive, background)

    objects = [
        {
            "item_uid": uid,
            "object": text(node, "ObjectName"),
            "label": text(node, "Name"),
            "resolved": logic.resolve(archive, uid),
            "element": node.tag,
        }
        for uid, node in archive.plan_objects_by_plan.get(plan_uid, [])
    ]
    payload["objects_total"] = len(objects)
    payload["objects"] = objects
    return payload


def image_reference(archive: FscpArchive, value: str) -> dict[str, Any]:
    """Ссылка на картинку: guid записи Content/ либо ресурс Global Monitor."""
    if "/" in value or value.lower().endswith(".png"):
        return {"kind": "app_resource", "value": value, "in_archive": False}
    info = archive.images.get(value.lower())
    if info is None:
        return {"kind": "content", "guid": value.lower(), "in_archive": False}
    return {"kind": "content", "in_archive": True, **info.as_dict()}


def plan_tree(archive: FscpArchive) -> list[dict[str, Any]]:
    def node(plan_uid: str) -> dict[str, Any]:
        plan = archive.plans_by_uid[plan_uid]
        children = archive.plan_children.get(plan_uid, [])
        payload: dict[str, Any] = {
            "uid": plan_uid,
            "name": text(plan, "Name"),
            "objects": len(archive.plan_objects_by_plan.get(plan_uid, [])),
        }
        if children:
            payload["children"] = [node(uid) for uid in children]
        return payload

    return [node(uid) for uid in archive.plan_roots]


def tree_text(root: Device, max_depth: int, max_nodes: int = MAX_TREE_NODES) -> str:
    """Компактное текстовое дерево с жёстким потолком по узлам."""
    lines: list[str] = []
    truncated = False

    def walk(device: Device, depth: int, prefix: str) -> None:
        nonlocal truncated
        if len(lines) >= max_nodes:
            truncated = True
            return
        label = f"{device.name}" if device.address else device.driver.short
        lines.append(f"{prefix}{label}")
        if depth >= max_depth:
            if device.children:
                lines.append(f"{prefix}   … {len(device.children)} вложенных")
            return
        for child in device.children:
            walk(child, depth + 1, prefix + "  ")

    walk(root, 0, "")
    if truncated:
        lines.append(f"… обрезано на {max_nodes} узлах; сузьте root или max_depth")
    return "\n".join(lines)
