"""Инструменты MCP поверх разобранного архива .fscp.

Чтение и запись. Все коллекции отдаются страницами: в реальной конфигурации
5656 устройств и 4649 объектов на планах, целиком они в контекст модели не
помещаются.

Правки копятся в открытой сессии и на диск не попадают, пока не вызван
fscp_save, - а он всегда пишет в новый файл. Исходную конфигурацию сервер
не перезаписывает: она единственная, а объект действующий.
"""

from __future__ import annotations

import csv
import functools
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import archive as arch
from . import drivers, edits, paging, skeleton, validate, views
from .archive import FscpError, text

server = MCPServer(
    name="fscp",
    version="0.1.0",
    instructions=(
        "Чтение и правка конфигураций .fscp (СПЗ «Рубеж-Глобал»). Начните с "
        "fscp_open, дальше работайте по handle. Устройства адресуются либо "
        "UID, либо адресом вида 1.2.1.1. Подложки планов не возвращайте "
        "инлайном без нужды — выгружайте через extract_plan_image и "
        "открывайте файлом. Правки копятся в памяти: диск не меняется, пока "
        "не вызван fscp_save, и он всегда пишет в новый файл. fscp_diff "
        "показывает накопленное, fscp_revert откатывает."
    ),
)


def tool(*args: Any, **kwargs: Any):
    """Как server.tool(), но FscpError отдаётся текстом, а не трейсбеком."""

    def decorate(function):
        @functools.wraps(function)
        def wrapper(*call_args: Any, **call_kwargs: Any):
            try:
                return function(*call_args, **call_kwargs)
            except FscpError as exc:
                return {"error": str(exc)}

        return server.tool(*args, **kwargs)(wrapper)

    return decorate


# -------------------------------------------------------------- сохранение


@tool(
    description=(
        "Создать новую пустую конфигурацию .fscp и сразу открыть её — вернётся "
        "handle. donor — путь к существующему .fscp, из которого побайтово "
        "переносится SecurityConfiguration.xml (учётные записи): сервер его не "
        "разбирает и не показывает. Без донора запись просто отсутствует, и "
        "пользователей заводят в самом Global Monitor — примет ли он такой "
        "файл, не проверено."
    )
)
def fscp_create(
    path: str, donor: str | None = None, overwrite: bool = False
) -> dict[str, Any]:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    if target.is_dir():
        raise FscpError(f"{target} - это каталог; укажите имя файла .fscp")
    if target.exists() and not overwrite:
        raise FscpError(
            f"{target} уже существует; передайте overwrite=true, чтобы заменить"
        )

    source = None
    if donor:
        source = Path(donor).expanduser()
        if not source.is_absolute():
            source = (Path.cwd() / source).resolve()

    result = skeleton.create(target, donor=source)
    handle, archive = arch.open_archive(target)
    return {
        "handle": handle,
        **result,
        **archive.summary(),
        "hint": (
            "дерево пустое: добавляйте ГК через add_device на «Локальную сеть». "
            "Шаблон снят с текущей версии Global Monitor; если ваша новее, "
            "надёжнее создать пустой файл в ней самой"
        ),
    }



@tool(
    description=(
        "Сохранить конфигурацию с правками в НОВЫЙ файл .fscp. Исходник не "
        "трогается никогда; существующий файл по пути out_path перезаписывается "
        "только при overwrite=true. Записи, которых правка не касалась, "
        "копируются побайтово. Перед записью идёт проверка целостности — "
        "отключается через check=false."
    )
)
def fscp_save(
    handle: str, out_path: str, overwrite: bool = False, check: bool = True
) -> dict[str, Any]:
    archive = arch.session(handle)
    target = Path(out_path).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    if target.is_dir():
        raise FscpError(f"{target} - это каталог; укажите имя файла .fscp")

    if check:
        problems = validate.preflight(archive)
        if problems:
            listing = "; ".join(f"{p.code}: {p.message}" for p in problems[:5])
            more = f" и ещё {len(problems) - 5}" if len(problems) > 5 else ""
            raise FscpError(
                f"сохранение отменено, нарушений целостности: {len(problems)}. "
                f"{listing}{more}. Полный список - validate_config; "
                "check=false запишет файл как есть"
            )

    result = archive.save(target, overwrite=overwrite)
    result["edits"] = len(archive.journal)
    result["hint"] = (
        "проверять результат надо открытием в Global Monitor - сервер этого не "
        "умеет. Сессия осталась открытой, правки в ней на месте"
    )
    return result


@tool(
    description=(
        "Что накоплено в сессии: список правок по-русски и какие записи архива "
        "будут перезаписаны при сохранении."
    )
)
def fscp_diff(
    handle: str, offset: int = 0, limit: int = paging.DEFAULT_LIMIT
) -> dict[str, Any]:
    archive = arch.session(handle)
    payload = paging.page(
        [views.edit_entry(edit) for edit in archive.journal], offset, limit, key="edits"
    )
    payload["source"] = str(archive.path)
    payload["dirty_entries"] = sorted(archive.dirty)
    if not archive.journal:
        payload["hint"] = "правок нет; сохранённый файл был бы копией исходного"
    return payload


@tool(
    description=(
        "Откатить все правки сессии к состоянию на момент открытия архива. "
        "Частичный откат не поддерживается."
    )
)
def fscp_revert(handle: str) -> dict[str, Any]:
    archive = arch.session(handle)
    return archive.revert()


# ------------------------------------------------------------------ сессия


@tool(description="Открыть файл .fscp и получить handle для остальных вызовов.")
def fscp_open(path: str) -> dict[str, Any]:
    handle, archive = arch.open_archive(path)
    return {
        "handle": handle,
        **archive.summary(),
        "entries": [e["name"] for e in archive.entries],
        "note": (
            "SecurityConfiguration.xml не читается (хеши паролей); "
            "System/Layouts только перечислены."
        ),
    }


@tool(description="Закрыть архив и освободить память сессии.")
def fscp_close(handle: str) -> dict[str, Any]:
    return {"closed": arch.close(handle)}


@tool(description="Сводка по открытому архиву: версии, счётчики, список ГК, записи ZIP.")
def fscp_info(handle: str) -> dict[str, Any]:
    archive = arch.session(handle)
    return {
        **archive.summary(),
        "gk": [
            {"uid": gk.uid, "name": gk.name, "ip": gk.property_value("IPAddress")}
            for gk in archive.gk_devices
        ],
        "entries": archive.entries,
        "unparsed": [*arch.UNPARSED_CONFIGS, arch.SECURITY_CONFIG],
    }


# --------------------------------------------------------------- устройства


@tool(description="Дети узла дерева устройств. Без parent — корень конфигурации.")
def list_devices(
    handle: str,
    parent: str | None = None,
    driver: str | None = None,
    offset: int = 0,
    limit: int = paging.DEFAULT_LIMIT,
) -> dict[str, Any]:
    archive = arch.session(handle)
    node = archive.device(parent) if parent else archive.root_device
    if node is None:
        raise FscpError("в конфигурации нет дерева устройств")

    children = node.children
    if driver:
        needle = driver.casefold()
        children = [c for c in children if needle in c.driver.short.casefold()]

    return {
        "parent": {"uid": node.uid, "name": node.name, "address": node.address},
        **paging.page([c.brief() for c in children], offset, limit, key="devices"),
    }


@tool(description="Полная карточка устройства по UID или адресу вида 1.2.1.1.")
def get_device(handle: str, device: str) -> dict[str, Any]:
    archive = arch.session(handle)
    return views.device_detail(archive, archive.device(device))


@tool(
    description=(
        "Поиск устройств по типу (короткое имя драйвера), префиксу адреса, "
        "описанию или значению свойства."
    )
)
def search_devices(
    handle: str,
    driver: str | None = None,
    address_prefix: str | None = None,
    description: str | None = None,
    property_name: str | None = None,
    property_value: str | None = None,
    offset: int = 0,
    limit: int = paging.DEFAULT_LIMIT,
) -> dict[str, Any]:
    archive = arch.session(handle)
    if not any((driver, address_prefix, description, property_name, property_value)):
        raise FscpError("задайте хотя бы один критерий поиска")

    hits = []
    for device in archive.devices_by_uid.values():
        if driver and driver.casefold() not in device.driver.short.casefold():
            continue
        if address_prefix and not device.address.startswith(address_prefix):
            continue
        if description and description.casefold() not in device.description.casefold():
            continue
        if property_name is not None or property_value is not None:
            props = views.properties(device.element)
            if property_name is not None:
                match = {k: v for k, v in props.items() if property_name.casefold() in k.casefold()}
            else:
                match = props
            if not match:
                continue
            if property_value is not None and not any(
                property_value.casefold() in v.casefold() for v in match.values()
            ):
                continue
        hits.append(device)

    hits.sort(key=lambda d: _address_key(d.address))
    return paging.page([d.brief() for d in hits], offset, limit, key="devices")


def _address_key(address: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in address.split("."))
    except ValueError:
        return (10**9,)


@tool(description="Компактное текстовое дерево устройств с потолком по глубине и узлам.")
def device_tree(
    handle: str,
    root: str | None = None,
    max_depth: int = 3,
    max_nodes: int = views.MAX_TREE_NODES,
) -> dict[str, Any]:
    archive = arch.session(handle)
    node = archive.device(root) if root else archive.root_device
    if node is None:
        raise FscpError("в конфигурации нет дерева устройств")
    capped = max(1, min(max_nodes, views.MAX_TREE_NODES))
    return {
        "root": node.name or node.driver.short,
        "max_depth": max_depth,
        "tree": views.tree_text(node, max_depth, capped),
    }


# ------------------------------------------------------- правка устройств


@tool(
    description=(
        "Добавить устройство под указанный узел дерева. driver — короткое имя "
        "из list_drivers или UID драйвера. Без int_address берётся первый "
        "свободный адрес на линии. count>1 добавляет несколько подряд."
    )
)
def add_device(
    handle: str,
    parent: str,
    driver: str,
    int_address: int | None = None,
    description: str = "",
    count: int = 1,
) -> dict[str, Any]:
    archive = arch.session(handle)
    created = edits.add_device(
        archive,
        parent=parent,
        driver=driver,
        int_address=int_address,
        description=description,
        count=count,
    )
    return {
        "added": len(created),
        "devices": [archive.devices_by_uid[uid].brief() for uid in created],
        "hint": "правка в памяти; на диск её положит fscp_save",
    }


@tool(
    description=(
        "Изменить поля устройства: описание, IntAddress, отключение, серийный "
        "номер, привязку к зонам, свойства GKProperty. Задавайте только то, что "
        "меняете. Подписи на планах обновляются сами."
    )
)
def set_device(
    handle: str,
    device: str,
    description: str | None = None,
    int_address: int | None = None,
    is_disabled: bool | None = None,
    serial_no: str | None = None,
    zones: list[str] | None = None,
    properties: dict[str, str] | None = None,
) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.set_device(
        archive,
        device=device,
        description=description,
        int_address=int_address,
        is_disabled=is_disabled,
        serial_no=serial_no,
        zones=zones,
        properties=properties,
    )


@tool(
    description=(
        "Перенести устройство вместе с поддеревом под другой узел — например, "
        "перевесить прибор на другую АЛС. Без int_address адрес выбирается "
        "свободный на новой линии."
    )
)
def move_device(
    handle: str, device: str, new_parent: str, int_address: int | None = None
) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.move_device(
        archive, device=device, new_parent=new_parent, int_address=int_address
    )


@tool(
    description=(
        "Удалить устройство вместе с поддеревом. Если на него ссылаются зоны, "
        "логика или объекты на планах, вызов отказывает и перечисляет ссылки; "
        "force=true удаляет вместе с ними."
    )
)
def remove_device(handle: str, device: str, force: bool = False) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.remove_device(archive, device=device, force=force)


# ------------------------------------------------------- зоны, сценарии и пр.


@tool(description="Объекты верхнего уровня: zone, guard_zone, scenario, direction, mpt, door и др.")
def list_objects(
    handle: str,
    kind: str = "zone",
    query: str | None = None,
    offset: int = 0,
    limit: int = paging.DEFAULT_LIMIT,
) -> dict[str, Any]:
    archive = arch.session(handle)
    if kind not in archive.objects_by_kind:
        raise FscpError(
            f"неизвестный вид '{kind}'; доступны: {', '.join(archive.objects_by_kind)}"
        )
    items = archive.objects_by_kind[kind]
    if query:
        needle = query.casefold()
        items = [i for i in items if needle in i.name.casefold()]
    return paging.page([i.brief() for i in items], offset, limit, key="objects")


@tool(description="Карточка объекта верхнего уровня (зона, сценарий, направление) по UID.")
def get_object(handle: str, uid: str) -> dict[str, Any]:
    archive = arch.session(handle)
    return views.object_detail(archive, archive.object(uid))


@tool(description="Что за объект скрывается за GUID: устройство, зона, план, подложка, драйвер.")
def resolve_uid(handle: str, uid: str) -> dict[str, Any]:
    archive = arch.session(handle)
    key = uid.strip().lower()

    device = archive.devices_by_uid.get(key)
    if device is not None:
        return {"kind": "device", **device.brief(), "path": views.device_detail(archive, device)["path"]}

    ref = archive.objects_by_uid.get(key)
    if ref is not None:
        return {"kind": ref.kind, **ref.brief()}

    plan = archive.plans_by_uid.get(key)
    if plan is not None:
        return {"kind": "plan", "uid": key, "name": text(plan, "Name")}

    info = archive.images.get(key)
    if info is not None:
        return {"kind": "plan_image", **info.as_dict(), "used_by_plans": archive.image_refs.get(key, [])}

    driver = drivers.get(key)
    if driver is not drivers.UNKNOWN:
        return {"kind": "driver", **driver.as_dict()}

    placements = archive.plan_objects_by_item.get(key)
    if placements:
        return {"kind": "plan_object_target", "uid": key, "on_plans": views.plan_placements(archive, key)}

    return {"kind": "unknown", "uid": key, "note": "GUID не найден ни в одном индексе"}


# ----------------------------------------------- правка объектов и логики


@tool(
    description=(
        "Создать объект верхнего уровня: zone (зона) или scenario (сценарий). "
        "Номер No назначается автоматически — следующий свободный. Виды, для "
        "которых схема не снята с рабочих конфигураций, создать нельзя."
    )
)
def add_object(
    handle: str,
    kind: str,
    name: str,
    description: str = "",
    fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    archive = arch.session(handle)
    uid = edits.add_object(
        archive, kind=kind, name=name, description=description, fields=fields
    )
    return {
        "uid": uid,
        **views.object_detail(archive, archive.objects_by_uid[uid]),
        "hint": "правка в памяти; на диск её положит fscp_save",
    }


@tool(
    description=(
        "Изменить поля объекта: имя, описание, Fire1Count/Fire2Count у зоны, "
        "DelayTime/Hold/DelayRegime у сценария. Номер No не меняется никогда — "
        "он входит в отображаемое имя и на него ссылаются."
    )
)
def set_object(
    handle: str,
    uid: str,
    name: str | None = None,
    description: str | None = None,
    fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.set_object(
        archive, uid=uid, name=name, description=description, fields=fields
    )


@tool(
    description=(
        "Удалить зону или сценарий, вычистив ссылки на него: привязки "
        "устройств, условия логики, объекты на планах. Без force отказывает и "
        "перечисляет, кто ссылается."
    )
)
def remove_object(handle: str, uid: str, force: bool = False) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.remove_object(archive, uid=uid, force=force)


@tool(
    description=(
        "Добавить условие в логику устройства или объекта. group — "
        "OnClausesGroup, OffClausesGroup, OnNowClausesGroup, OffNowClausesGroup "
        "или StopClausesGroup. state принимается кодом (Fire2) или по-русски "
        "(Пожар2). Список для целей и название операции выбираются по виду "
        "целей; every=true даёт «во всех» вместо «в любом из»."
    )
)
def add_clause(
    handle: str,
    owner: str,
    targets: list[str],
    state: str = "Fire2",
    group: str = "OnClausesGroup",
    every: bool = False,
    join: str = "Or",
    tag: str = "Logic",
) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.add_clause(
        archive,
        owner=owner,
        targets=targets,
        state=state,
        group=group,
        every=every,
        join=join,
        tag=tag,
    )


@tool(
    description=(
        "Убрать логику: указанную группу условий целиком либо, без group, все "
        "группы владельца."
    )
)
def clear_logic(
    handle: str, owner: str, group: str | None = None, tag: str = "Logic"
) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.clear_logic(archive, owner=owner, group=group, tag=tag)


# ------------------------------------------------------------------- планы


@tool(description="Дерево планов с числом объектов на каждом.")
def list_plans(handle: str) -> dict[str, Any]:
    archive = arch.session(handle)
    return {"total": len(archive.plans_by_uid), "plans": views.plan_tree(archive)}


@tool(description="План со списком нарисованных на нём объектов (страницами).")
def get_plan(
    handle: str,
    plan_uid: str,
    offset: int = 0,
    limit: int = paging.DEFAULT_LIMIT,
) -> dict[str, Any]:
    archive = arch.session(handle)
    key = plan_uid.strip().lower()
    if key not in archive.plans_by_uid:
        raise FscpError(f"не найден план {plan_uid}")
    detail = views.plan_detail(archive, key)
    objects = detail.pop("objects")
    detail.pop("objects_total", None)
    return {**detail, **paging.page(objects, offset, limit, key="objects")}


@tool(description="На каких планах нарисован объект с данным UID.")
def find_on_plans(handle: str, uid: str) -> dict[str, Any]:
    archive = arch.session(handle)
    key = uid.strip().lower()
    placements = views.plan_placements(archive, key)
    return {"uid": key, "name": _resolve_name(archive, key), "total": len(placements), "placements": placements}


def _resolve_name(archive: arch.FscpArchive, uid: str) -> str:
    from . import logic

    return logic.resolve(archive, uid)


# ------------------------------------------------------------ правка планов


@tool(
    description=(
        "Создать план. parent_uid вкладывает его в другой план. image_path — "
        "картинка PNG или JPEG с диска: она кладётся в Content/ архива, а "
        "размеры плана берутся из её заголовка."
    )
)
def add_plan(
    handle: str,
    name: str,
    parent_uid: str | None = None,
    width: float = 297,
    height: float = 210,
    image_path: str | None = None,
    description: str = "",
) -> dict[str, Any]:
    archive = arch.session(handle)
    uid = edits.add_plan(
        archive,
        name=name,
        parent_uid=parent_uid,
        width=width,
        height=height,
        image_path=image_path,
        description=description,
    )
    return {
        "uid": uid,
        **views.plan_detail(archive, uid),
        "hint": "правка в памяти; на диск её положит fscp_save",
    }


@tool(description="Изменить имя, описание или размеры плана.")
def set_plan(
    handle: str,
    plan_uid: str,
    name: str | None = None,
    description: str | None = None,
    width: float | None = None,
    height: float | None = None,
) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.set_plan(
        archive,
        plan_uid=plan_uid,
        name=name,
        description=description,
        width=width,
        height=height,
    )


@tool(
    description=(
        "Удалить план вместе с вложенными в него и всеми нарисованными "
        "объектами. Без force отказывает, если удалять есть что."
    )
)
def remove_plan(handle: str, plan_uid: str, force: bool = False) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.remove_plan(archive, plan_uid=plan_uid, force=force)


@tool(
    description=(
        "Нарисовать объект ГК на плане. Устройства кладутся точкой (left, top), "
        "зоны и сценарии — прямоугольником (нужны ещё width и height). Связь "
        "ставится с обеих сторон."
    )
)
def place_object(
    handle: str,
    plan_uid: str,
    item: str,
    left: float = 0,
    top: float = 0,
    width: float | None = None,
    height: float | None = None,
) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.place_object(
        archive,
        plan_uid=plan_uid,
        item=item,
        left=left,
        top=top,
        width=width,
        height=height,
    )


@tool(description="Убрать объект с плана, сняв связь с обеих сторон.")
def remove_placement(handle: str, plan_uid: str, item: str) -> dict[str, Any]:
    archive = arch.session(handle)
    return edits.remove_placement(archive, plan_uid=plan_uid, item=item)


# --------------------------------------------------------------- подложки


@tool(description="Подложки планов из Content/: размер, разрешение, кто ссылается, сироты.")
def list_plan_images(handle: str) -> dict[str, Any]:
    archive = arch.session(handle)
    items = []
    for guid, info in sorted(archive.images.items()):
        plan_uids = archive.image_refs.get(guid, [])
        items.append(
            {
                **info.as_dict(),
                "used_by": [
                    text(archive.plans_by_uid[p], "Name")
                    for p in plan_uids
                    if p in archive.plans_by_uid
                ],
                "orphan": not plan_uids,
            }
        )
    return {
        "total": len(items),
        "images": items,
        "orphans": [i["guid"] for i in items if i["orphan"]],
        "broken_refs": archive.missing_image_refs,
        "app_resources": sorted(archive.resource_refs),
    }


@tool(description="Выгрузить подложку плана в файл на диск (вне архива) для просмотра.")
def extract_plan_image(handle: str, guid: str, out_path: str) -> dict[str, Any]:
    archive = arch.session(handle)
    info = archive.images.get(guid.strip().lower())
    if info is None:
        raise FscpError(f"в архиве нет подложки {guid}")

    target = Path(out_path).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    if target.is_dir():
        suffix = {"image/png": ".png", "image/jpeg": ".jpg"}.get(info.media_type, ".bin")
        target = target / f"{info.guid}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(archive.blob(info.guid))

    return {
        "path": str(target),
        **info.as_dict(),
        "hint": "откройте файл обычным чтением — оно само ужмёт картинку",
    }


@tool(
    description=(
        "Инлайновое превью подложки. Требует установленной Pillow "
        "(pip install -e .[img]); max_px обязателен — оригиналы бывают 5000x4749."
    )
)
def get_plan_image(handle: str, guid: str, max_px: int = 900) -> Any:
    archive = arch.session(handle)
    info = archive.images.get(guid.strip().lower())
    if info is None:
        raise FscpError(f"в архиве нет подложки {guid}")
    try:
        from io import BytesIO

        from PIL import Image as PILImage
    except ImportError:
        return {
            "error": "нужна Pillow: .venv/Scripts/python.exe -m pip install -e \".[img]\"",
            "alternative": "используйте extract_plan_image и откройте файл",
        }

    max_px = max(64, min(max_px, 2000))
    with PILImage.open(BytesIO(archive.blob(info.guid))) as picture:
        picture.thumbnail((max_px, max_px))
        buffer = BytesIO()
        picture.convert("RGB").save(buffer, format="PNG", optimize=True)
    return Image(data=buffer.getvalue(), format="png")


# ------------------------------------------------------- справочник и сырьё


@tool(description="Справочник драйверов Рубеж; query фильтрует по имени, описанию, категории.")
def list_drivers(
    handle: str | None = None,
    query: str = "",
    offset: int = 0,
    limit: int = paging.DEFAULT_LIMIT,
) -> dict[str, Any]:
    found = drivers.find(query)
    return paging.page([d.as_dict() for d in found], offset, limit, key="drivers")


@tool(
    description=(
        "Сырой фрагмент XML по пути ElementTree (например Zones/GKZone[1]). "
        "Запасной путь, когда типизированных инструментов не хватает."
    )
)
def read_xml(
    handle: str,
    path: str,
    config: str = arch.GK_CONFIG,
    max_chars: int = 4000,
) -> dict[str, Any]:
    archive = arch.session(handle)
    if config == arch.GK_CONFIG:
        root = archive.gk
    elif config == arch.PLANS_CONFIG:
        if archive.plans is None:
            raise FscpError("в архиве нет PlansConfiguration.xml")
        root = archive.plans
    else:
        raise FscpError(
            f"в охвате только {arch.GK_CONFIG} и {arch.PLANS_CONFIG}; "
            f"{arch.SECURITY_CONFIG} не читается намеренно"
        )

    found = root.findall(path) if path else [root]
    if not found:
        raise FscpError(f"по пути '{path}' ничего не найдено")

    chunks = [ET.tostring(node, encoding="unicode") for node in found[:5]]
    body, truncated = paging.clip("\n".join(chunks), max_chars)
    return {
        "matched": len(found),
        "returned": min(len(found), 5),
        "truncated": truncated,
        "xml": body,
    }


@tool(description="Выгрузить плоский список устройств в CSV по указанному пути вне архива.")
def export_devices_csv(handle: str, out_path: str) -> dict[str, Any]:
    archive = arch.session(handle)
    target = Path(out_path).expanduser()
    if not target.is_absolute():
        target = (Path.cwd() / target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    rows = sorted(archive.devices_by_uid.values(), key=lambda d: _address_key(d.address))
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(
            ["address", "name", "driver", "driver_description", "description",
             "uid", "parent_address", "zones", "disabled"]
        )
        for device in rows:
            zones = "; ".join(
                z["name"] for z in views.named_uids(archive, device.element, "ZoneUIDs")
            )
            writer.writerow(
                [
                    device.address,
                    device.name,
                    device.driver.short,
                    device.driver.description,
                    device.description,
                    device.uid,
                    device.parent.address if device.parent else "",
                    zones,
                    text(device.element, "IsDisabled"),
                ]
            )
    return {"path": str(target), "rows": len(rows)}


@tool(
    description=(
        "Проверка целостности: устаревшие подписи на планах, битые ссылки на "
        "подложки и объекты, сироты в Content/."
    )
)
def validate_config(handle: str, limit: int = 20) -> dict[str, Any]:
    archive = arch.session(handle)

    stale: list[dict[str, str]] = []
    renamed: list[dict[str, str]] = []
    dangling: list[dict[str, str]] = []

    for uid, placements in archive.plan_objects_by_item.items():
        device = archive.devices_by_uid.get(uid)
        known = device is not None or uid in archive.objects_by_uid
        for plan_uid, node in placements:
            label = text(node, "Name")
            plan_name = text(archive.plans_by_uid[plan_uid], "Name")
            if not known:
                dangling.append({"uid": uid, "label": label, "plan": plan_name})
                continue
            if device is None or not label or label == device.name:
                continue

            entry = {
                "uid": uid,
                "label_on_plan": label,
                "actual": device.name,
                "plan": plan_name,
            }
            # Подпись без адреса устройства — устаревший адрес. Если адрес тот
            # же, а расходится только начало строки, дело в названии типа.
            if device.address and device.address in label:
                renamed.append(entry)
            else:
                stale.append(entry)

    orphans = [g for g in archive.images if not archive.image_refs.get(g)]
    return {
        "stale_plan_labels": {
            "total": len(stale),
            "note": (
                "подпись объекта кэшируется при отрисовке; после перенумерации "
                "АЛС она расходится с фактическим адресом устройства"
            ),
            "examples": stale[:limit],
        },
        "driver_name_mismatch": {
            "total": len(renamed),
            "note": (
                "адрес совпал, разошлось название типа: справочник в "
                "справочник drivers.json именует драйвер иначе, чем Global Monitor"
            ),
            "examples": renamed[:limit],
        },
        "dangling_plan_objects": {"total": len(dangling), "examples": dangling[:limit]},
        "broken_image_refs": archive.missing_image_refs,
        "orphan_images": orphans,
        **_integrity(archive, limit),
    }


def _integrity(archive: arch.FscpArchive, limit: int) -> dict[str, Any]:
    """Проверки целостности, которые сторожат запись.

    Ошибки блокируют fscp_save, предупреждения - нет: висячие ссылки и
    незнакомые драйверы встречаются и в файлах, записанных самим
    Global Monitor.
    """
    report = validate.inspect(archive)
    return {
        "errors": {
            "total": len(report["errors"]),
            "note": "пока они есть, fscp_save откажется писать файл",
            "examples": report["errors"][:limit],
        },
        "warnings": {
            "total": len(report["warnings"]),
            "examples": report["warnings"][:limit],
        },
    }
