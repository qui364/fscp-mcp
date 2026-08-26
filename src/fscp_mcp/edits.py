"""Мутации разобранной конфигурации и журнал правок.

Правки копятся в открытой сессии, диск не трогается до fscp_save. Отсюда
требование ко всем функциям этого модуля: менять дерево **на месте** -
вставлять, удалять и переписывать текст узлов, но никогда не пересобирать
дерево целиком. На идентичности узлов держится исключение вложенных планов в
FscpArchive._index_plans, да и снимок для отката привязан к записи архива,
а не к конкретным объектам.

Порядок в каждой мутации один: проверить предусловия -> поправить дерево ->
пометить запись архива грязной -> переиндексировать -> дописать в журнал.
Переиндексация полная: на самой большой конфигурации это 0,16 с, дешевле
инкрементальной инвалидации и без класса ошибок «индекс разошёлся с деревом».

Следствие переиндексации: объекты Device и ObjectRef после неё пересоздаются.
Держать ссылку на Device через мутацию нельзя - только перезапрашивать по UID.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import drivers, images, logic, schema
from .archive import CONTENT_PREFIX, GK_CONFIG, NIL_UID, PLANS_CONFIG
from .errors import FscpError

if TYPE_CHECKING:
    from .archive import Device, FscpArchive


@dataclass(frozen=True, slots=True)
class Edit:
    """Одна правка в журнале сессии.

    name хранится строкой, а не ссылкой: к моменту показа журнала объект может
    быть уже удалён, а пользователю нужно понимать, что именно он делал.
    """

    seq: int
    op: str
    kind: str
    uid: str
    name: str
    detail: str
    entry: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def record(
    archive: FscpArchive,
    *,
    op: str,
    kind: str,
    uid: str,
    name: str,
    detail: str,
    entry: str,
) -> Edit:
    """Помечает запись архива грязной и дописывает правку в журнал."""
    archive.dirty.add(entry)
    edit = Edit(
        seq=len(archive.journal) + 1,
        op=op,
        kind=kind,
        uid=uid,
        name=name,
        detail=detail,
        entry=entry,
    )
    archive.journal.append(edit)
    return edit


def normalize_text(value: str) -> str:
    """Приводит перевод строки к \n перед записью в узел.

    ElementTree при разборе нормализует \r\n в \n, поэтому текст с CRLF не
    пережил бы круг «записали - прочитали» без расхождения. Нормализуем на
    входе мутации, а не на выходе сериализатора: тогда round-trip нетронутого
    файла остаётся побайтовым.
    """
    return value.replace("\r\n", "\n").replace("\r", "\n")


def require_clean_uid(archive: FscpArchive, uid: str) -> str:
    """Проверяет, что UID ещё никем не занят."""
    key = uid.strip().lower()
    if key in archive.devices_by_uid or key in archive.objects_by_uid:
        raise FscpError(f"UID {key} уже занят в этой конфигурации")
    if key in archive.plans_by_uid:
        raise FscpError(f"UID {key} уже занят планом")
    return key


# ---------------------------------------------------------------- устройства


def _children(device: ET.Element) -> ET.Element:
    """Контейнер Children, создаётся по схеме, если его нет."""
    found = device.find("Children")
    if found is None:
        found = schema.insert_ordered(
            device, "Children", None, fields=schema.DEVICE_FIELDS
        )
    return found


def _int_address(element: ET.Element) -> int:
    try:
        return int((element.findtext("IntAddress") or "0").strip() or 0)
    except ValueError:
        return 0


def _addressable(element: ET.Element) -> bool:
    """no_address-драйверы (ГК, «Группа реле», «Линия БМП») своего адреса не имеют.

    Поэтому они законно делят IntAddress с соседями и в правилах уникальности
    и порядка не участвуют.
    """
    return not drivers.get((element.findtext("DriverUID") or "").strip().lower()).no_address


def taken_addresses(parent: ET.Element, driver_uid: str) -> set[int]:
    """Занятые адреса среди братьев того же типа.

    Уникален не IntAddress сам по себе, а пара (DriverUID, IntAddress): у БМП
    рядом стоят «Линия БМП», БМПК и БМПП с одним адресом, у КАУ - АЛС и
    индикатор неисправности. Проверено на 8 рабочих конфигурациях: наивное
    правило нарушается 42 раза, это - ни разу.
    """
    driver_uid = driver_uid.strip().lower()
    return {
        _int_address(child)
        for child in _children(parent).findall("GKDevice")
        if (child.findtext("DriverUID") or "").strip().lower() == driver_uid
    }


def line_addresses(archive: FscpArchive, host: Device) -> set[int]:
    """Последние компоненты адресов, уже занятые на линии узла.

    Считать по одним лишь братьям нельзя: дети сквозного узла (МВК4, РМ2, АМ4)
    стоят на одном уровне с ним, и их адреса тоже заняты. В синтетике АМ4 с
    IntAddress=3 держит ребёнка на 1.2.1.4, так что свободный адрес на линии -
    5, а не 4. Опираемся на уже построенный devices_by_address, а не считаем
    заново.
    """
    prefix = f"{host.address}." if host.address else ""
    taken: set[int] = set()
    for address in archive.devices_by_address:
        if prefix and not address.startswith(prefix):
            continue
        tail = address[len(prefix) :]
        if tail.isdigit():
            taken.add(int(tail))
    return taken


def free_address(archive: FscpArchive, host: Device, driver_uid: str) -> int:
    """Первый адрес, свободный и на линии, и среди братьев того же типа."""
    taken = line_addresses(archive, host) | taken_addresses(host.element, driver_uid)
    candidate = 1
    while candidate in taken:
        candidate += 1
    return candidate


def insert_sorted(container: ET.Element, element: ET.Element) -> int:
    """Вставляет устройство так, чтобы дети остались по возрастанию IntAddress.

    Порядок не косметика: проверен на 1798 многодетных узлах восьми рабочих
    конфигураций - ни одного нарушения, значит Global Monitor на него
    рассчитывает. Узлы без собственного адреса в правило не входят и остаются
    там, где стояли, поэтому новое устройство встаёт сразу за последним
    адресуемым соседом с меньшим адресом, а не в конец контейнера.
    """
    address = _int_address(element)
    target = None
    for index, child in enumerate(container):
        if not _addressable(child):
            continue
        if _int_address(child) <= address:
            target = index + 1
        else:
            target = index
            break
    if target is None:
        container.append(element)
        return len(container) - 1
    container.insert(target, element)
    return target


def _names(archive: FscpArchive) -> dict[str, str]:
    return {uid: device.name for uid, device in archive.devices_by_uid.items()}


def refresh_plan_labels(archive: FscpArchive, before: dict[str, str]) -> int:
    """Обновляет кэш подписей на планах для устройств, у которых сменилось имя.

    Поле Name объекта плана - это сохранённая при отрисовке подпись, а не
    ссылка. Её расхождение с вычисленным именем ловит validate_config как
    «устаревшую подпись». Не обновив её после правки адреса или описания, мы
    своими руками произвели бы ровно тот дефект, который сами же и
    диагностируем.
    """
    updated = 0
    for uid, name in _names(archive).items():
        if before.get(uid) == name:
            continue
        for _, node in archive.plan_objects_by_item.get(uid, []):
            label = node.find("Name")
            if label is not None and (label.text or "") != name:
                label.text = name
                updated += 1
    if updated:
        archive.dirty.add(PLANS_CONFIG)
    return updated


def add_device(
    archive: FscpArchive,
    parent: str,
    driver: str,
    int_address: int | None = None,
    description: str = "",
    count: int = 1,
) -> list[str]:
    """Добавляет одно или несколько устройств под указанный узел."""
    if count < 1 or count > 256:
        raise FscpError(f"count={count}: добавлять можно от 1 до 256 устройств")

    host = archive.device(parent)
    driver_uid = _resolve_driver(driver)
    container = _children(host.element)

    if int_address is not None and int_address < 0:
        raise FscpError(f"IntAddress={int_address}: адрес не может быть отрицательным")

    before = _names(archive)
    created: list[str] = []
    address = (
        int_address
        if int_address is not None
        else free_address(archive, host, driver_uid)
    )

    for step in range(count):
        current = address + step
        taken = taken_addresses(host.element, driver_uid)
        if current in taken:
            raise FscpError(
                f"на {host.name} уже есть {drivers.get(driver_uid).short} с "
                f"IntAddress={current}; укажите другой адрес или опустите его"
            )
        element = schema.new_device(driver_uid, current, description=description)
        insert_sorted(container, element)
        created.append((element.findtext("UID") or "").lower())

    archive.dirty.add(GK_CONFIG)
    archive._reindex()
    relabelled = refresh_plan_labels(archive, before)

    for uid in created:
        device = archive.devices_by_uid[uid]
        record(
            archive,
            op="add",
            kind="device",
            uid=uid,
            name=device.name,
            detail=(
                f"под {host.name}, IntAddress={device.int_address}"
                + (f", подписей на планах обновлено: {relabelled}" if relabelled else "")
            ),
            entry=GK_CONFIG,
        )
    return created


def _resolve_driver(driver: str) -> str:
    """Драйвер по UID или по короткому имени - как его видит пользователь."""
    key = driver.strip().lower()
    if key in drivers.table():
        return key
    matches = [d for d in drivers.table().values() if d.short.casefold() == key.casefold()]
    if len(matches) == 1:
        return matches[0].uid
    if not matches:
        found = drivers.find(driver)[:5]
        hint = ", ".join(d.short for d in found) or "ничего похожего"
        raise FscpError(f"драйвер '{driver}' не найден; похоже на: {hint}")
    raise FscpError(
        f"'{driver}' подходит нескольким драйверам: "
        + ", ".join(f"{d.short} ({d.uid})" for d in matches)
        + "; укажите UID"
    )


def _guid_list(parent: ET.Element, tag: str, uids: list[str]) -> None:
    """Переписывает список <guid> целиком, сохраняя место поля по схеме."""
    container = schema.insert_ordered(parent, tag, None, fields=schema.DEVICE_FIELDS)
    for child in list(container):
        container.remove(child)
    container.text = None
    for uid in uids:
        ET.SubElement(container, "guid").text = uid


def set_device(
    archive: FscpArchive,
    device: str,
    description: str | None = None,
    int_address: int | None = None,
    is_disabled: bool | None = None,
    serial_no: str | None = None,
    zones: list[str] | None = None,
    properties: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Меняет поля устройства. Не переданное остаётся как было."""
    target = archive.device(device)
    element = target.element
    parent = target.parent
    before = _names(archive)
    changes: list[str] = []

    if int_address is not None:
        if int_address < 0:
            raise FscpError(
                f"IntAddress={int_address}: адрес не может быть отрицательным"
            )
        if parent is None:
            raise FscpError(f"{target.name}: у корня конфигурации нет адреса")
        occupied = taken_addresses(parent.element, target.driver_uid)
        occupied.discard(target.int_address)
        if int_address in occupied:
            raise FscpError(
                f"на {parent.name} уже есть {target.driver.short} с "
                f"IntAddress={int_address}"
            )
        changes.append(f"IntAddress: {target.int_address} -> {int_address}")
        schema.set_device_field(element, "IntAddress", str(int_address))
        container = _children(parent.element)
        container.remove(element)
        insert_sorted(container, element)

    if description is not None:
        was = target.description
        changes.append(f"описание: {was or 'нет'} -> {description or 'нет'}")
        schema.set_device_field(element, "Description", normalize_text(description))

    if is_disabled is not None:
        changes.append(f"отключено: {str(is_disabled).lower()}")
        schema.set_device_field(element, "IsDisabled", str(is_disabled).lower())

    if serial_no is not None:
        changes.append(f"серийный номер: {serial_no}")
        schema.set_device_field(element, "SerialNo", serial_no)

    if zones is not None:
        resolved = [_zone_uid(archive, z) for z in zones]
        changes.append(f"зон привязано: {len(resolved)}")
        _guid_list(element, "ZoneUIDs", resolved)

    if properties is not None:
        changes.append(_set_properties(element, properties))

    if not changes:
        raise FscpError("не задано ни одного поля для изменения")

    archive.dirty.add(GK_CONFIG)
    archive._reindex()
    relabelled = refresh_plan_labels(archive, before)

    fresh = archive.devices_by_uid[target.uid]
    detail = "; ".join(changes)
    if relabelled:
        detail += f"; подписей на планах обновлено: {relabelled}"
    record(
        archive,
        op="set",
        kind="device",
        uid=fresh.uid,
        name=fresh.name,
        detail=detail,
        entry=GK_CONFIG,
    )
    return {
        "uid": fresh.uid,
        "name": fresh.name,
        "address": fresh.address,
        "changes": changes,
        "plan_labels_updated": relabelled,
    }


def _zone_uid(archive: FscpArchive, value: str) -> str:
    """Зона по UID или по имени вида «1.Склад»."""
    key = value.strip().lower()
    if key in archive.objects_by_uid:
        return key
    for ref in archive.objects_by_kind.get("zone", []):
        if ref.name.casefold() == value.strip().casefold():
            return ref.uid
    raise FscpError(f"зона '{value}' не найдена")


def _set_properties(element: ET.Element, properties: dict[str, str]) -> str:
    """Правит GKProperty устройства, создавая недостающие."""
    container = schema.insert_ordered(
        element, "Properties", None, fields=schema.DEVICE_FIELDS
    )
    existing = {
        (node.findtext("Name") or "").strip(): node
        for node in container.findall("GKProperty")
    }
    for name, value in properties.items():
        node = existing.get(name)
        if node is None:
            node = ET.SubElement(container, "GKProperty")
            ET.SubElement(node, "Name").text = name
            ET.SubElement(node, "Value").text = str(value)
            continue
        field = node.find("StringValue")
        if field is None:
            field = node.find("Value")
        if field is None:
            field = ET.SubElement(node, "Value")
        field.text = str(value)
    return f"свойств изменено: {len(properties)}"


def move_device(
    archive: FscpArchive, device: str, new_parent: str, int_address: int | None = None
) -> dict[str, Any]:
    """Переносит устройство вместе с поддеревом под другой узел."""
    target = archive.device(device)
    host = archive.device(new_parent)
    if target.parent is None:
        raise FscpError("корень конфигурации переносить нельзя")
    if target.uid == host.uid:
        raise FscpError("устройство нельзя перенести само в себя")

    ancestors = {host.uid}
    walk = host.parent
    while walk is not None:
        ancestors.add(walk.uid)
        walk = walk.parent
    if target.uid in ancestors:
        raise FscpError(
            f"{host.name} лежит внутри {target.name}; перенос создал бы петлю"
        )

    was_parent = target.parent.name
    was_address = target.address
    before = _names(archive)

    address = (
        int_address
        if int_address is not None
        else free_address(archive, host, target.driver_uid)
    )
    occupied = taken_addresses(host.element, target.driver_uid)
    if address in occupied:
        raise FscpError(
            f"на {host.name} уже есть {target.driver.short} с IntAddress={address}"
        )

    _children(target.parent.element).remove(target.element)
    schema.set_device_field(target.element, "IntAddress", str(address))
    insert_sorted(_children(host.element), target.element)

    archive.dirty.add(GK_CONFIG)
    archive._reindex()
    relabelled = refresh_plan_labels(archive, before)

    fresh = archive.devices_by_uid[target.uid]
    record(
        archive,
        op="move",
        kind="device",
        uid=fresh.uid,
        name=fresh.name,
        detail=(
            f"{was_parent} ({was_address}) -> {host.name} ({fresh.address})"
            + (f"; подписей на планах обновлено: {relabelled}" if relabelled else "")
        ),
        entry=GK_CONFIG,
    )
    return {
        "uid": fresh.uid,
        "name": fresh.name,
        "address": fresh.address,
        "was": was_address,
        "plan_labels_updated": relabelled,
    }


def subtree_uids(device: Device) -> list[str]:
    """UID устройства и всех его потомков."""
    found = [device.uid]
    for child in device.children:
        found.extend(subtree_uids(child))
    return found


def remove_device(
    archive: FscpArchive, device: str, force: bool = False
) -> dict[str, Any]:
    """Удаляет устройство вместе с поддеревом.

    Без force отказывает, если на устройство или его потомков кто-то ссылается,
    и перечисляет кто именно: молча оставленная висячая ссылка - это то, обо
    что Global Monitor споткнётся уже на объекте.
    """
    target = archive.device(device)
    if target.parent is None:
        raise FscpError("корень конфигурации удалить нельзя")

    doomed = subtree_uids(target)
    incoming = {uid: archive.referrers(uid) for uid in doomed}
    total = sum(len(v) for v in incoming.values())

    if total and not force:
        where = []
        for uid, refs in incoming.items():
            if not refs:
                continue
            name = archive.devices_by_uid[uid].name
            tags = ", ".join(sorted({r.tag for r in refs}))
            where.append(f"{name}: {len(refs)} ({tags})")
        raise FscpError(
            f"на {target.name} и его потомков ссылаются {total} раз - "
            + "; ".join(where[:5])
            + ". Передайте force=true, чтобы удалить вместе со ссылками"
        )

    cleaned = 0
    for uid in doomed:
        cleaned += drop_references(archive, uid)
    pruned = prune_empty_clauses(archive)

    name = target.name
    _children(target.parent.element).remove(target.element)
    archive.dirty.add(GK_CONFIG)
    archive._reindex()

    record(
        archive,
        op="remove",
        kind="device",
        uid=target.uid,
        name=name,
        detail=(
            f"удалено устройств: {len(doomed)}"
            + (f", вычищено ссылок: {cleaned}" if cleaned else "")
            + (f", убрано опустевших условий: {pruned}" if pruned else "")
        ),
        entry=GK_CONFIG,
    )
    return {
        "removed": len(doomed),
        "references_cleaned": cleaned,
        "clauses_pruned": pruned,
        "name": name,
    }


def drop_references(archive: FscpArchive, uid: str) -> int:
    """Вычищает все ссылки на объект: списки, скаляры и объекты на планах."""
    dropped = 0
    for ref in archive.referrers(uid):
        if ref.tag == "ItemUID":
            # Объекту плана без цели нечего показывать - убираем его целиком.
            owner = _owner_of(archive.plans, ref.holder)
            if owner is not None:
                owner.remove(ref.holder)
                archive.dirty.add(PLANS_CONFIG)
                dropped += 1
            continue
        if ref.in_list:
            ref.holder.remove(ref.element)
        else:
            ref.element.text = NIL_UID
        archive.dirty.add(ref.entry)
        dropped += 1
    return dropped


def _owner_of(root: ET.Element | None, node: ET.Element) -> ET.Element | None:
    """Родитель узла: ElementTree обратных ссылок не держит."""
    if root is None:
        return None
    for parent in root.iter():
        for child in parent:
            if child is node:
                return parent
    return None


# --------------------------------------------------------------- объекты ГК


def _container(archive: FscpArchive, kind: str) -> ET.Element:
    name = schema.OBJECT_CONTAINERS.get(kind)
    if name is None:
        known = ", ".join(sorted(schema.OBJECT_KINDS))
        raise FscpError(
            f"создавать объекты вида '{kind}' нельзя: схема известна только для "
            f"{known}. Для остальных видов ни в одной доступной конфигурации нет "
            "ни одного образца, а угаданная структура сломает файл молча"
        )
    found = archive.gk.find(name)
    if found is None:
        raise FscpError(f"в конфигурации нет контейнера {name}")
    return found


def next_no(container: ET.Element) -> int:
    """Следующий свободный номер объекта.

    No - это устойчивый идентификатор, а не позиция: в рабочих конфигурациях
    он не отсортирован и идёт с дырами (263 зоны при максимальном номере 1025).
    Поэтому берём max+1 и никогда не перенумеровываем существующие: номер
    входит в отображаемое имя объекта, и сдвиг переименовал бы всех соседей.
    """
    numbers = []
    for element in container:
        try:
            numbers.append(int((element.findtext("No") or "0").strip() or 0))
        except ValueError:
            continue
    return max(numbers, default=0) + 1


def add_object(
    archive: FscpArchive,
    kind: str,
    name: str,
    description: str = "",
    fields: dict[str, str] | None = None,
) -> str:
    """Создаёт зону или сценарий."""
    if not name.strip():
        raise FscpError("имя объекта не может быть пустым")

    container = _container(archive, kind)
    _, allowed, _ = schema.OBJECT_KINDS[kind]

    element = schema.new_object(
        kind,
        normalize_text(name.strip()),
        next_no(container),
        description=normalize_text(description),
    )
    for field, value in (fields or {}).items():
        if field not in allowed:
            raise FscpError(
                f"у вида '{kind}' нет поля {field}; есть: {', '.join(allowed)}"
            )
        schema.insert_ordered(element, field, str(value), fields=allowed)

    container.append(element)
    archive.dirty.add(GK_CONFIG)
    archive._reindex()

    uid = (element.findtext("UID") or "").lower()
    ref = archive.objects_by_uid[uid]
    record(
        archive,
        op="add",
        kind=kind,
        uid=uid,
        name=ref.name,
        detail=f"No={ref.no}",
        entry=GK_CONFIG,
    )
    return uid


def set_object(
    archive: FscpArchive,
    uid: str,
    name: str | None = None,
    description: str | None = None,
    fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Меняет поля объекта. Номер No не меняется никогда."""
    ref = archive.object(uid)
    if ref.kind not in schema.OBJECT_KINDS:
        raise FscpError(
            f"править объекты вида '{ref.kind}' пока нельзя: их схема не снята "
            "ни с одной доступной конфигурации"
        )
    _, allowed, _ = schema.OBJECT_KINDS[ref.kind]
    changes: list[str] = []

    if name is not None:
        if not name.strip():
            raise FscpError("имя объекта не может быть пустым")
        changes.append(f"имя: {ref.element.findtext('Name')} -> {name.strip()}")
        schema.insert_ordered(
            ref.element, "Name", normalize_text(name.strip()), fields=allowed
        )

    if description is not None:
        clean = normalize_text(description)
        if clean:
            schema.insert_ordered(ref.element, "Description", clean, fields=allowed)
        else:
            existing = ref.element.find("Description")
            if existing is not None:
                ref.element.remove(existing)
        changes.append(f"описание: {clean or 'нет'}")

    for field, value in (fields or {}).items():
        if field in ("UID", "No"):
            raise FscpError(f"{field} менять нельзя: на него ссылаются другие объекты")
        if field not in allowed:
            raise FscpError(
                f"у вида '{ref.kind}' нет поля {field}; есть: {', '.join(allowed)}"
            )
        changes.append(f"{field}: {ref.element.findtext(field)} -> {value}")
        schema.insert_ordered(ref.element, field, str(value), fields=allowed)

    if not changes:
        raise FscpError("не задано ни одного поля для изменения")

    archive.dirty.add(GK_CONFIG)
    archive._reindex()
    fresh = archive.objects_by_uid[ref.uid]
    record(
        archive,
        op="set",
        kind=fresh.kind,
        uid=fresh.uid,
        name=fresh.name,
        detail="; ".join(changes),
        entry=GK_CONFIG,
    )
    return {"uid": fresh.uid, "name": fresh.name, "changes": changes}


def remove_object(
    archive: FscpArchive, uid: str, force: bool = False
) -> dict[str, Any]:
    """Удаляет объект и, при force, все ссылки на него."""
    ref = archive.object(uid)
    incoming = archive.referrers(ref.uid)

    if incoming and not force:
        tags = ", ".join(sorted({r.tag for r in incoming}))
        raise FscpError(
            f"на {ref.name} ссылаются {len(incoming)} раз ({tags}). Передайте "
            "force=true, чтобы удалить вместе со ссылками"
        )

    cleaned = drop_references(archive, ref.uid)
    pruned = prune_empty_clauses(archive)
    container = _container(archive, ref.kind)
    container.remove(ref.element)

    name = ref.name
    archive.dirty.add(GK_CONFIG)
    archive._reindex()
    record(
        archive,
        op="remove",
        kind=ref.kind,
        uid=ref.uid,
        name=name,
        detail=(
            (f"вычищено ссылок: {cleaned}" if cleaned else "ссылок не было")
            + (f", убрано опустевших условий: {pruned}" if pruned else "")
        ),
        entry=GK_CONFIG,
    )
    return {
        "uid": ref.uid,
        "name": name,
        "references_cleaned": cleaned,
        "clauses_pruned": pruned,
    }


# -------------------------------------------------------------------- логика


def _logic_owner(archive: FscpArchive, owner: str) -> tuple[ET.Element, str, str]:
    """Владелец логики - устройство или объект ГК. Возвращает узел, имя и вид."""
    key = owner.strip().lower()
    if key in archive.objects_by_uid:
        ref = archive.objects_by_uid[key]
        return ref.element, ref.name, ref.kind
    device = archive.device(owner)
    return device.element, device.name, "device"


def _logic_block(element: ET.Element, tag: str) -> ET.Element:
    """Узел <Logic> или <NSLogic> владельца, при нужде создаётся по схеме."""
    if tag not in ("Logic", "NSLogic"):
        raise FscpError(f"логика бывает Logic или NSLogic, не '{tag}'")
    found = element.find(tag)
    if found is not None:
        return found
    if element.tag == "GKDevice":
        block = schema.insert_ordered(element, tag, None, fields=schema.DEVICE_FIELDS)
    else:
        block = ET.SubElement(element, tag)
    ET.SubElement(block, "UseOffCounterLogic").text = "true"
    return block


def _group_node(block: ET.Element, group: str, join: str) -> ET.Element:
    """Группа условий внутри логики; создаётся на своём месте по схеме.

    Группы идут ДО UseOffCounterLogic - порядок снят с рабочих конфигураций,
    и XmlSerializer на перепутанном молча теряет поле.
    """
    found = block.find(group)
    if found is not None:
        return found
    fresh = schema.clause_group(join)
    fresh.tag = group
    block.insert(schema.position(schema.LOGIC_ORDER, block, group), fresh)
    return fresh


def _target_kind(archive: FscpArchive, uid: str) -> str:
    key = uid.strip().lower()
    if key in archive.devices_by_uid:
        return "device"
    if key in archive.objects_by_uid:
        return archive.objects_by_uid[key].kind
    raise FscpError(f"цель {uid} не найдена: ни устройство, ни объект ГК")


def _state_code(state: str) -> str:
    """Состояние принимается и кодом (Fire2), и по-русски (Пожар2)."""
    if state in logic.STATE_TYPES:
        return state
    for code, russian in logic.STATE_TYPES.items():
        if russian.casefold() == state.strip().casefold():
            return code
    known = ", ".join(f"{c} ({r})" for c, r in logic.STATE_TYPES.items())
    raise FscpError(f"неизвестное состояние '{state}'; бывают: {known}")


def add_clause(
    archive: FscpArchive,
    owner: str,
    targets: list[str],
    state: str = "Fire2",
    group: str = "OnClausesGroup",
    every: bool = False,
    join: str = "Or",
    tag: str = "Logic",
) -> dict[str, Any]:
    """Добавляет условие в логику владельца.

    Список, в который лягут цели, и название операции выбираются по виду целей:
    зоны идут в ZoneUIDs с операцией AnyZone, сценарии - в DelayUIDs с AnyDelay
    и так далее. Смешивать виды в одном условии нельзя - в рабочих
    конфигурациях такого не встречается, и что с этим сделает Global Monitor,
    неизвестно.
    """
    if group not in logic.CLAUSE_GROUPS:
        known = ", ".join(logic.CLAUSE_GROUPS)
        raise FscpError(f"группа '{group}' не бывает; есть: {known}")
    if join not in logic.JOIN:
        raise FscpError(f"join бывает Or или And, не '{join}'")
    if not targets:
        raise FscpError("условие без целей Global Monitor прочитать не сможет")

    kinds = {_target_kind(archive, uid) for uid in targets}
    if len(kinds) > 1:
        raise FscpError(
            "в одном условии цели должны быть одного вида, а тут "
            + ", ".join(sorted(kinds))
            + "; заведите отдельное условие на каждый вид"
        )
    kind = kinds.pop()
    if kind not in schema.CLAUSE_TARGETS:
        raise FscpError(f"цели вида '{kind}' в условиях не участвуют")

    field, any_op, all_op = schema.CLAUSE_TARGETS[kind]
    operation = all_op if every else any_op
    state_code = _state_code(state)

    element, name, owner_kind = _logic_owner(archive, owner)
    block = _logic_block(element, tag)
    node = _group_node(block, group, join)
    clauses = node.find("Clauses")
    if clauses is None:
        clauses = schema.insert_ordered(
            node, "Clauses", None, fields=schema.CLAUSE_GROUP_FIELDS
        )

    clause = schema.new_clause(state_code, operation)
    container = clause.find(field)
    for uid in targets:
        ET.SubElement(container, "guid").text = uid.strip().lower()
    clauses.append(clause)

    archive.dirty.add(GK_CONFIG)
    archive._reindex()

    rendered = logic.render(archive, element, tag=tag)
    record(
        archive,
        op="add",
        kind="clause",
        uid=(element.findtext("UID") or "").lower(),
        name=name,
        detail=(
            f"{logic.CLAUSE_GROUPS[group]}: {logic.STATE_TYPES[state_code]} "
            f"{logic.OPERATIONS[operation]}, целей {len(targets)}"
        ),
        entry=GK_CONFIG,
    )
    return {"owner": name, "kind": owner_kind, "logic": rendered}


def clear_logic(
    archive: FscpArchive, owner: str, group: str | None = None, tag: str = "Logic"
) -> dict[str, Any]:
    """Убирает группу условий целиком либо всю логику владельца."""
    element, name, _ = _logic_owner(archive, owner)
    block = element.find(tag)
    if block is None:
        raise FscpError(f"у {name} нет логики {tag}")

    if group is not None and group not in logic.CLAUSE_GROUPS:
        raise FscpError(f"группа '{group}' не бывает")

    removed = []
    for node in list(block):
        if node.tag not in logic.CLAUSE_GROUPS:
            continue
        if group is not None and node.tag != group:
            continue
        block.remove(node)
        removed.append(logic.CLAUSE_GROUPS[node.tag])

    if not removed:
        raise FscpError(f"у {name} нечего убирать в {tag}")

    archive.dirty.add(GK_CONFIG)
    archive._reindex()
    record(
        archive,
        op="remove",
        kind="logic",
        uid=(element.findtext("UID") or "").lower(),
        name=name,
        detail="убрано: " + ", ".join(removed),
        entry=GK_CONFIG,
    )
    return {"owner": name, "removed": removed}


def _count(parent: ET.Element, tag: str) -> int:
    """Сколько детей в подэлементе. Пустая проверка Element устарела в ET."""
    found = parent.find(tag)
    return 0 if found is None else len(found)


def prune_empty_clauses(archive: FscpArchive) -> int:
    """Убирает условия, у которых после каскада не осталось ни одной цели.

    Условие без целей - самое вероятное из того, обо что Global Monitor может
    споткнуться: смысла у него нет, а в рабочих конфигурациях таких не
    встречается ни одного. Опустевшая после этого группа условий убирается
    следом.
    """
    removed = 0
    for owner in archive.gk.iter():
        for group in list(owner):
            if group.tag not in logic.CLAUSE_GROUPS:
                continue
            clauses = group.find("Clauses")
            if clauses is None:
                continue
            for clause in list(clauses.findall("GKClause")):
                if any(_count(clause, field) for field in logic.UID_FIELDS):
                    continue
                clauses.remove(clause)
                removed += 1
            if len(clauses) == 0 and _count(group, "ClauseGroups") == 0:
                owner.remove(group)
    if removed:
        archive.dirty.add(GK_CONFIG)
    return removed


# --------------------------------------------------------------------- планы


def _plans_root(archive: FscpArchive) -> ET.Element:
    if archive.plans is None:
        raise FscpError("в архиве нет PlansConfiguration.xml - планов не бывает")
    container = archive.plans.find("Plans")
    if container is None:
        raise FscpError("в PlansConfiguration.xml нет контейнера Plans")
    return container


def _plan_element(archive: FscpArchive, plan_uid: str) -> ET.Element:
    element = archive.plans_by_uid.get(plan_uid.strip().lower())
    if element is None:
        raise FscpError(f"плана {plan_uid} нет в конфигурации")
    return element


def _plan_name(element: ET.Element) -> str:
    return (element.findtext("Name") or "").strip()


def add_plan(
    archive: FscpArchive,
    name: str,
    parent_uid: str | None = None,
    width: float = 297,
    height: float = 210,
    image_path: str | None = None,
    description: str = "",
) -> str:
    """Создаёт план, при нужде вкладывая его в другой и кладя подложку."""
    if not name.strip():
        raise FscpError("имя плана не может быть пустым")

    if parent_uid:
        parent = _plan_element(archive, parent_uid)
        container = parent.find("Children")
        if container is None:
            container = schema.insert_ordered(
                parent, "Children", None, fields=schema.PLAN_FIELDS
            )
    else:
        container = _plans_root(archive)

    background = ""
    source_name = ""
    if image_path:
        background, source_name, width, height = _store_background(
            archive, image_path, width, height
        )

    element = schema.new_plan(
        normalize_text(name.strip()),
        width,
        height,
        description=normalize_text(description),
        background=background,
        source_name=source_name,
    )
    container.append(element)

    archive.dirty.add(PLANS_CONFIG)
    archive._reindex()

    uid = (element.findtext("UID") or "").lower()
    record(
        archive,
        op="add",
        kind="plan",
        uid=uid,
        name=_plan_name(element),
        detail=(
            f"{schema._number(width)}x{schema._number(height)}"
            + (f", подложка {background}" if background else ", без подложки")
        ),
        entry=PLANS_CONFIG,
    )
    return uid


def _store_background(
    archive: FscpArchive, image_path: str, width: float, height: float
) -> tuple[str, str, float, float]:
    """Кладёт картинку в Content/<guid> и отдаёт ссылку на неё.

    Тип определяется по сигнатуре, а не по расширению: в архиве блобы лежат
    вообще без расширения, и Global Monitor узнаёт их так же. Размеры плана
    берутся из заголовка картинки, если вызывающий их не задал.
    """
    source = Path(image_path).expanduser()
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    if not source.is_file():
        raise FscpError(f"файл подложки не найден: {source}")

    payload = source.read_bytes()
    info = images.describe(
        guid=schema.new_uid(),
        entry="",
        size_bytes=len(payload),
        head=payload[: images.HEAD_BYTES],
    )
    if info.media_type not in ("image/png", "image/jpeg"):
        raise FscpError(
            f"{source.name}: подложкой бывают PNG и JPEG, а тут {info.media_type}"
        )

    entry = f"{CONTENT_PREFIX}{info.guid}"
    archive.added_entries[entry] = payload
    archive.images[info.guid] = replace(info, entry=entry)

    if info.width and info.height:
        width, height = float(info.width), float(info.height)
    return info.guid, source.name, width, height


def set_plan(
    archive: FscpArchive,
    plan_uid: str,
    name: str | None = None,
    description: str | None = None,
    width: float | None = None,
    height: float | None = None,
) -> dict[str, Any]:
    """Меняет поля плана."""
    element = _plan_element(archive, plan_uid)
    changes: list[str] = []

    if name is not None:
        if not name.strip():
            raise FscpError("имя плана не может быть пустым")
        changes.append(f"имя: {_plan_name(element)} -> {name.strip()}")
        schema.insert_ordered(
            element, "Name", normalize_text(name.strip()), fields=schema.PLAN_FIELDS
        )

    if description is not None:
        clean = normalize_text(description)
        if clean:
            schema.insert_ordered(
                element, "Description", clean, fields=schema.PLAN_FIELDS
            )
        else:
            existing = element.find("Description")
            if existing is not None:
                element.remove(existing)
        changes.append(f"описание: {clean or 'нет'}")

    for field, value in (("Width", width), ("Height", height)):
        if value is None:
            continue
        changes.append(f"{field}: {element.findtext(field)} -> {value}")
        schema.insert_ordered(
            element, field, schema._number(value), fields=schema.PLAN_FIELDS
        )

    if not changes:
        raise FscpError("не задано ни одного поля для изменения")

    archive.dirty.add(PLANS_CONFIG)
    archive._reindex()
    record(
        archive,
        op="set",
        kind="plan",
        uid=plan_uid.strip().lower(),
        name=_plan_name(element),
        detail="; ".join(changes),
        entry=PLANS_CONFIG,
    )
    return {"uid": plan_uid.strip().lower(), "name": _plan_name(element),
            "changes": changes}


def remove_plan(
    archive: FscpArchive, plan_uid: str, force: bool = False
) -> dict[str, Any]:
    """Удаляет план вместе с вложенными и всеми нарисованными объектами."""
    key = plan_uid.strip().lower()
    element = _plan_element(archive, key)

    nested = _plan_subtree(archive, key)
    drawn = sum(len(archive.plan_objects_by_plan.get(uid, [])) for uid in nested)

    if (len(nested) > 1 or drawn) and not force:
        raise FscpError(
            f"на плане {_plan_name(element)} и вложенных в него ({len(nested) - 1}) "
            f"нарисовано объектов: {drawn}. Передайте force=true, чтобы удалить "
            "вместе с ними"
        )

    # Встречная сторона связи живёт у объектов ГК: их PlanElementUIDs держат
    # UID нарисованных объектов, и без чистки они повиснут.
    unlinked = 0
    for uid in nested:
        for _, node in archive.plan_objects_by_plan.get(uid, []):
            unlinked += _unlink_placement(archive, node)

    parent_uid = archive.plan_parent.get(key)
    if parent_uid is None:
        _plans_root(archive).remove(element)
    else:
        parent = _plan_element(archive, parent_uid)
        children = parent.find("Children")
        if children is not None:
            children.remove(element)

    name = _plan_name(element)
    archive.dirty.add(PLANS_CONFIG)
    archive._reindex()
    record(
        archive,
        op="remove",
        kind="plan",
        uid=key,
        name=name,
        detail=(
            f"планов удалено: {len(nested)}, объектов на них: {drawn}"
            + (f", снято привязок: {unlinked}" if unlinked else "")
        ),
        entry=PLANS_CONFIG,
    )
    return {"uid": key, "name": name, "plans_removed": len(nested),
            "objects_removed": drawn, "links_removed": unlinked}


def _plan_subtree(archive: FscpArchive, plan_uid: str) -> list[str]:
    found = [plan_uid]
    for child in archive.plan_children.get(plan_uid, []):
        found.extend(_plan_subtree(archive, child))
    return found


def _unlink_placement(archive: FscpArchive, node: ET.Element) -> int:
    """Убирает UID объекта плана из PlanElementUIDs объекта ГК.

    Связь двусторонняя: у объекта плана ItemUID смотрит на объект ГК, а у
    объекта ГК PlanElementUIDs держит UID объекта плана. Односторонняя связь -
    это тот самый висячий GUID, который потом некому объяснить.
    """
    placement_uid = (node.findtext("UID") or "").strip().lower()
    item_uid = (node.findtext("ItemUID") or "").strip().lower()
    if not placement_uid or not item_uid:
        return 0

    owner = archive.devices_by_uid.get(item_uid) or archive.objects_by_uid.get(item_uid)
    if owner is None:
        return 0
    container = owner.element.find("PlanElementUIDs")
    if container is None:
        return 0

    removed = 0
    for guid in list(container.findall("guid")):
        if (guid.text or "").strip().lower() == placement_uid:
            container.remove(guid)
            removed += 1
    if removed:
        archive.dirty.add(GK_CONFIG)
    return removed


def place_object(
    archive: FscpArchive,
    plan_uid: str,
    item: str,
    left: float = 0,
    top: float = 0,
    width: float | None = None,
    height: float | None = None,
) -> dict[str, Any]:
    """Рисует объект ГК на плане, связывая обе стороны.

    Устройства кладутся точками, зоны и сценарии - прямоугольниками: так
    поступает и сам Global Monitor во всех просмотренных конфигурациях.
    """
    plan = _plan_element(archive, plan_uid)
    owner, kind, label = _placement_target(archive, item)

    if kind not in schema.PLACEMENT:
        raise FscpError(
            f"объекты вида '{kind}' на планах не рисуются: в рабочих "
            "конфигурациях таких не встречается"
        )
    tag, container_tag, _, _ = schema.PLACEMENT[kind]

    container = plan.find(container_tag)
    if container is None:
        container = schema.insert_ordered(
            plan, container_tag, None, fields=schema.PLAN_FIELDS
        )

    element = schema.new_placement(
        kind, owner.uid, label, left, top, width=width, height=height
    )
    container.append(element)

    # Встречная сторона связи.
    links = owner.element.find("PlanElementUIDs")
    if links is None:
        fields = (
            schema.DEVICE_FIELDS
            if owner.element.tag == "GKDevice"
            else schema.OBJECT_KINDS[kind][1]
        )
        links = schema.insert_ordered(
            owner.element, "PlanElementUIDs", None, fields=fields
        )
    ET.SubElement(links, "guid").text = (element.findtext("UID") or "").lower()

    archive.dirty.add(PLANS_CONFIG)
    archive.dirty.add(GK_CONFIG)
    archive._reindex()

    record(
        archive,
        op="link",
        kind="plan_object",
        uid=(element.findtext("UID") or "").lower(),
        name=label,
        detail=f"нарисован на «{_plan_name(plan)}» как {tag} в ({left}, {top})",
        entry=PLANS_CONFIG,
    )
    return {
        "uid": (element.findtext("UID") or "").lower(),
        "item": owner.uid,
        "name": label,
        "plan": _plan_name(plan),
        "element": tag,
    }


def _placement_target(archive: FscpArchive, item: str) -> tuple[Any, str, str]:
    """Объект ГК, его вид и подпись, которая ляжет на план."""
    key = item.strip().lower()
    if key in archive.objects_by_uid:
        ref = archive.objects_by_uid[key]
        return ref, ref.kind, ref.name
    device = archive.device(item)
    return device, "device", device.name


def remove_placement(
    archive: FscpArchive, plan_uid: str, item: str
) -> dict[str, Any]:
    """Убирает объект с плана, снимая обе стороны связи."""
    plan = _plan_element(archive, plan_uid)
    owner, kind, label = _placement_target(archive, item)

    drawn = [
        node
        for uid, node in archive.plan_objects_by_plan.get(plan_uid.strip().lower(), [])
        if uid == owner.uid
    ]
    if not drawn:
        raise FscpError(f"{label} на плане «{_plan_name(plan)}» не нарисован")

    unlinked = 0
    for node in drawn:
        unlinked += _unlink_placement(archive, node)
        holder = _owner_of(archive.plans, node)
        if holder is not None:
            holder.remove(node)

    archive.dirty.add(PLANS_CONFIG)
    archive._reindex()
    record(
        archive,
        op="unlink",
        kind="plan_object",
        uid=owner.uid,
        name=label,
        detail=f"убран с плана «{_plan_name(plan)}», снято привязок: {unlinked}",
        entry=PLANS_CONFIG,
    )
    return {"removed": len(drawn), "links_removed": unlinked, "name": label}
