"""Канонический порядок полей и заготовки элементов.

.NET XmlSerializer читает элементы по порядку следования в схеме. На чужом
порядке он обычно не падает - он **молча пропускает поле**. Файл откроется,
настройка потеряется, и никто не заметит. Поэтому новые элементы вставляются
только через insert_ordered, никогда через append.

Порядок и значения по умолчанию сняты с рабочих конфигураций, а не придуманы:
DEVICE_FIELDS проверен на 25 385 устройствах в 8 конфигурациях - каждый
встреченный набор полей является его подпоследовательностью, нарушений ноль.
Необязательные поля XmlSerializer опускает целиком, а не пишет пустыми:
Description отсутствует у большинства устройств, а не стоит как <Description />.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from uuid import uuid4

NIL_UID = "00000000-0000-0000-0000-000000000000"

#: Порядок полей GKDevice. Проверен на 25 385 устройствах, 0 нарушений.
DEVICE_FIELDS: tuple[str, ...] = (
    "UID",
    "No",
    "Description",
    "AllowMultipleVisualization",
    "PlanElementUIDs",
    "IsDisabled",
    "ReserveGkUID",
    "DriverUID",
    "IsInnerKau",
    "IntAddress",
    "PredefinedName",
    "Children",
    "Properties",
    "DeviceProperties",
    "ZoneUIDs",
    "GuardZoneUIDs",
    "Logic",
    "NSLogic",
    "IgnoreLogicValidation",
    "ProjectAddress",
    "GKReflectionItem",
    "BmpDevices",
    "AllowBeOutsideZone",
    "MirrorIsAllObjects",
    "KDDevices",
    "EnterZoneUID",
    "ExitZoneUID",
    "DevicePropertiesString",
    "HasSelfDependence",
    "UseReservedIP",
    "SerialNo",
    "CardAndSensorEnter",
)

#: Поля, которые XmlSerializer опускает, когда значение не задано.
DEVICE_OPTIONAL = frozenset(
    {"Description", "PredefinedName", "ProjectAddress", "DevicePropertiesString"}
)

#: Значения по умолчанию для нового устройства - самые частые в рабочих
#: конфигурациях. None означает пустой контейнер <Foo />.
DEVICE_DEFAULTS: dict[str, str | None] = {
    "No": "0",
    "AllowMultipleVisualization": "false",
    "PlanElementUIDs": None,
    "IsDisabled": "false",
    "ReserveGkUID": NIL_UID,
    "IsInnerKau": "false",
    "Children": None,
    "Properties": None,
    "DeviceProperties": None,
    "ZoneUIDs": None,
    "GuardZoneUIDs": None,
    "IgnoreLogicValidation": "false",
    "BmpDevices": None,
    "AllowBeOutsideZone": "false",
    "MirrorIsAllObjects": "false",
    "KDDevices": None,
    "EnterZoneUID": NIL_UID,
    "ExitZoneUID": NIL_UID,
    "HasSelfDependence": "false",
    "UseReservedIP": "false",
    "SerialNo": "0",
    "CardAndSensorEnter": "false",
}

#: Пустая логика: у 4986 из 5014 устройств <Logic> состоит ровно из этого.
LOGIC_FIELDS = ("UseOffCounterLogic",)

#: Списки, из которых состоит GKReflectionItem. Форма единственная на все
#: 5014 устройств - ни одного отклонения.
REFLECTION_LISTS = (
    "MirrorUsers",
    "GuardZoneUIDs",
    "DeviceUIDs",
    "DelayUIDs",
    "DirectionUIDs",
    "NSUIDs",
    "MPTUIDs",
)


def new_uid() -> str:
    return str(uuid4())


def _leaf(tag: str, value: str | None) -> ET.Element:
    element = ET.Element(tag)
    if value is not None:
        element.text = value
    return element


def position(fields: tuple[str, ...], parent: ET.Element, tag: str) -> int:
    """Индекс, на который надо вставить tag, чтобы порядок остался каноническим."""
    if tag not in fields:
        raise ValueError(f"поле {tag} не описано в схеме")
    rank = fields.index(tag)
    for index, child in enumerate(parent):
        if child.tag not in fields or fields.index(child.tag) > rank:
            return index
    return len(parent)


def insert_ordered(
    parent: ET.Element, tag: str, value: str | None = None, *, fields: tuple[str, ...]
) -> ET.Element:
    """Вставляет поле на его место по схеме и возвращает элемент.

    Если поле уже есть, переписывается его значение: два одноимённых элемента
    XmlSerializer читает как повтор и берёт последний, что почти наверняка не
    то, чего хотел вызывающий.
    """
    existing = parent.find(tag)
    if existing is not None:
        existing.text = value
        return existing
    element = _leaf(tag, value)
    parent.insert(position(fields, parent, tag), element)
    return element


def set_device_field(device: ET.Element, tag: str, value: str | None) -> None:
    """Ставит или убирает поле устройства с сохранением порядка.

    Необязательное поле с пустым значением удаляется целиком - именно так его
    пишет XmlSerializer, и оставленный <Description /> отличался бы от того,
    что записала бы сама программа.
    """
    if not value and tag in DEVICE_OPTIONAL:
        existing = device.find(tag)
        if existing is not None:
            device.remove(existing)
        return
    insert_ordered(device, tag, value, fields=DEVICE_FIELDS)


def new_device(
    driver_uid: str, int_address: int, *, uid: str = "", description: str = ""
) -> ET.Element:
    """Собирает <GKDevice> со всеми полями в каноническом порядке."""
    device = ET.Element("GKDevice")
    device.append(_leaf("UID", uid or new_uid()))

    for tag in DEVICE_FIELDS[1:]:
        if tag == "DriverUID":
            device.append(_leaf(tag, driver_uid))
        elif tag == "IntAddress":
            device.append(_leaf(tag, str(int_address)))
        elif tag == "Description":
            if description:
                device.append(_leaf(tag, description))
        elif tag in ("Logic", "NSLogic"):
            block = ET.SubElement(device, tag)
            ET.SubElement(block, "UseOffCounterLogic").text = "true"
        elif tag == "GKReflectionItem":
            block = ET.SubElement(device, tag)
            for name in REFLECTION_LISTS:
                ET.SubElement(block, name)
        elif tag in DEVICE_DEFAULTS:
            device.append(_leaf(tag, DEVICE_DEFAULTS[tag]))

    return device


# ------------------------------------------------------------ объекты ГК

#: Поля GKZone. Проверено на 1120 зонах, 0 нарушений порядка.
ZONE_FIELDS: tuple[str, ...] = (
    "UID",
    "No",
    "Name",
    "Description",
    "AllowMultipleVisualization",
    "PlanElementUIDs",
    "IsFireB",
    "FireBDelayTime",
    "Fire1Count",
    "Fire2Count",
)

ZONE_DEFAULTS: dict[str, str | None] = {
    "AllowMultipleVisualization": "false",
    "PlanElementUIDs": None,
    "IsFireB": "false",
    "FireBDelayTime": "30",
    "Fire1Count": "1",
    "Fire2Count": "2",
}

#: Поля GKDelay - в интерфейсе это «сценарий». Проверено на 398 объектах.
SCENARIO_FIELDS: tuple[str, ...] = (
    "UID",
    "No",
    "Name",
    "Description",
    "AllowMultipleVisualization",
    "PlanElementUIDs",
    "DelayTime",
    "Hold",
    "DelayRegime",
    "Logic",
    "DirectionDevices",
    "IsAutoGenerated",
    "IsAutoReset",
    "PumpStationUID",
    "DoorUID",
)

SCENARIO_DEFAULTS: dict[str, str | None] = {
    "AllowMultipleVisualization": "false",
    "PlanElementUIDs": None,
    "DelayTime": "0",
    "Hold": "0",
    "DelayRegime": "On",
    "DirectionDevices": None,
    "IsAutoGenerated": "false",
    "IsAutoReset": "false",
    "PumpStationUID": NIL_UID,
    "DoorUID": NIL_UID,
}

#: Тег элемента и набор полей по виду объекта. Виды, которых нет здесь,
#: создавать нельзя: ни в одной доступной конфигурации нет ни одного образца,
#: а угаданная структура сломает файл молча.
OBJECT_KINDS: dict[str, tuple[str, tuple[str, ...], dict[str, str | None]]] = {
    "zone": ("GKZone", ZONE_FIELDS, ZONE_DEFAULTS),
    "scenario": ("GKDelay", SCENARIO_FIELDS, SCENARIO_DEFAULTS),
}

#: Контейнер верхнего уровня по виду объекта.
OBJECT_CONTAINERS = {"zone": "Zones", "scenario": "Delays"}

# ----------------------------------------------------------------- логика

#: Порядок внутри <Logic>. Группы условий идут ДО UseOffCounterLogic -
#: перепутав, мы бы отдали XmlSerializer порядок, которого он не ждёт.
LOGIC_ORDER: tuple[str, ...] = (
    "OnClausesGroup",
    "OffClausesGroup",
    "OnNowClausesGroup",
    "OffNowClausesGroup",
    "StopClausesGroup",
    "UseOffCounterLogic",
    "RedIndicatorLogic",
    "GreenIndicatorLogic",
    "YellowIndicatorLogic",
)

#: Поля группы условий. Форма единственная на все 819 групп.
CLAUSE_GROUP_FIELDS: tuple[str, ...] = (
    "PimUID",
    "ClauseGroups",
    "Clauses",
    "CardClauses",
    "ClauseJoinOperationType",
    "ForceLogicOnKAU",
)

#: Поля GKClause. Форма единственная на все 442 условия восьми конфигураций.
CLAUSE_FIELDS: tuple[str, ...] = (
    "ClauseConditionType",
    "StateType",
    "DeviceUIDs",
    "ZoneUIDs",
    "GuardZoneUIDs",
    "DirectionUIDs",
    "DelayUIDs",
    "DoorUIDs",
    "MPTUIDs",
    "PumpStationsUIDs",
    "ClauseOperationType",
)

#: Куда класть цели и как назвать операцию - по виду объекта.
#: Первый элемент операции для «любого из», второй для «всех».
CLAUSE_TARGETS: dict[str, tuple[str, str, str]] = {
    "device": ("DeviceUIDs", "AnyDevice", "AllDevices"),
    "zone": ("ZoneUIDs", "AnyZone", "AllZones"),
    "guard_zone": ("GuardZoneUIDs", "AnyGuardZone", "AllGuardZones"),
    "direction": ("DirectionUIDs", "AnyDirection", "AllDirections"),
    "scenario": ("DelayUIDs", "AnyDelay", "AllDelays"),
    "door": ("DoorUIDs", "AnyDoor", "AllDoors"),
    "mpt": ("MPTUIDs", "AnyMPT", "AllMPTs"),
    "pump_station": ("PumpStationsUIDs", "AnyPumpStation", "AllPumpStations"),
}


def new_object(
    kind: str, name: str, no: int, *, uid: str = "", description: str = ""
) -> ET.Element:
    """Собирает объект верхнего уровня со всеми полями в порядке схемы."""
    if kind not in OBJECT_KINDS:
        raise ValueError(f"вид {kind} не описан в схеме")
    tag, fields, defaults = OBJECT_KINDS[kind]

    element = ET.Element(tag)
    for field in fields:
        if field == "UID":
            element.append(_leaf(field, uid or new_uid()))
        elif field == "No":
            element.append(_leaf(field, str(no)))
        elif field == "Name":
            element.append(_leaf(field, name))
        elif field == "Description":
            if description:
                element.append(_leaf(field, description))
        elif field == "Logic":
            block = ET.SubElement(element, field)
            ET.SubElement(block, "UseOffCounterLogic").text = "true"
        elif field in defaults:
            element.append(_leaf(field, defaults[field]))
    return element


def new_clause(state: str, operation: str) -> ET.Element:
    """Пустое условие: все списки целей на месте, заполняется нужный."""
    clause = ET.Element("GKClause")
    for field in CLAUSE_FIELDS:
        if field == "ClauseConditionType":
            ET.SubElement(clause, field).text = "If"
        elif field == "StateType":
            ET.SubElement(clause, field).text = state
        elif field == "ClauseOperationType":
            ET.SubElement(clause, field).text = operation
        else:
            ET.SubElement(clause, field)
    return clause


def clause_group(join: str = "Or") -> ET.Element:
    """Пустая группа условий - контейнер, в который кладутся GKClause."""
    group = ET.Element("group")
    for field in CLAUSE_GROUP_FIELDS:
        if field == "PimUID":
            ET.SubElement(group, field).text = NIL_UID
        elif field == "ClauseJoinOperationType":
            ET.SubElement(group, field).text = join
        elif field == "ForceLogicOnKAU":
            ET.SubElement(group, field).text = "false"
        else:
            ET.SubElement(group, field)
    return group


# ------------------------------------------------------------------- планы

#: Поля Plan. Проверено на 144 планах восьми конфигураций, 0 нарушений.
PLAN_FIELDS: tuple[str, ...] = (
    "UID",
    "No",
    "Name",
    "Description",
    "BackgroundColor",
    "BackgroundImageSource",
    "BackgroundSourceName",
    "BackgroundSVGImageSource",
    "Children",
    "ElementEllipses",
    "ElementPolygons",
    "ElementPolygonSubPlans",
    "ElementPolylines",
    "ElementRectangles",
    "ElementRectangleSubPlans",
    "ElementTextBlocks",
    "ElementTextBoxes",
    "Height",
    "ImageType",
    "IncidentUID",
    "LocationUID",
    "IsAsynchronousLoad",
    "IsNotShowPlan",
    "PointObjects",
    "PolygonObjects",
    "RectangleObjects",
    "Width",
)

#: Пустые контейнеры плана - фигуры, которые сервер не создаёт, но обязан
#: выписать: XmlSerializer ждёт их на своих местах.
PLAN_CONTAINERS = frozenset(
    {
        "Children",
        "ElementEllipses",
        "ElementPolygons",
        "ElementPolygonSubPlans",
        "ElementPolylines",
        "ElementRectangles",
        "ElementRectangleSubPlans",
        "ElementTextBlocks",
        "ElementTextBoxes",
        "PointObjects",
        "PolygonObjects",
        "RectangleObjects",
    }
)

#: Поля точечного объекта - так на планах рисуются устройства.
#: Проверено на 21 105 объектах, 0 нарушений.
POINT_FIELDS: tuple[str, ...] = (
    "UID",
    "No",
    "Name",
    "BorderColor",
    "BorderThickness",
    "BackgroundColor",
    "BackgroundImageSource",
    "BackgroundSVGImageSource",
    "ImageType",
    "ZIndex",
    "IsLocked",
    "IsEnabled",
    "IsHidden",
    "ZLayer",
    "Left",
    "Top",
    "ItemUID",
    "ModuleName",
    "ObjectName",
    "ModuleData",
    "DisplayStateChangeOnPlanTree",
)

#: Поля прямоугольника - так рисуются зоны и сценарии. Проверено на 742.
RECTANGLE_FIELDS: tuple[str, ...] = (
    "UID",
    "No",
    "Name",
    "BorderColor",
    "BorderThickness",
    "BackgroundColor",
    "BackgroundImageSource",
    "BackgroundSVGImageSource",
    "ImageType",
    "ZIndex",
    "IsLocked",
    "IsEnabled",
    "IsHidden",
    "ZLayer",
    "ImageSource",
    "Left",
    "Top",
    "Height",
    "Width",
    "ItemUID",
    "ModuleName",
    "ObjectName",
    "ModuleData",
    "DisplayStateChangeOnPlanTree",
)

#: ModuleName у всех 21 105 объектов один и тот же - вычислять нечего.
MODULE_NAME = "Групповой контроллер"

#: Какой фигурой рисуется объект и как называется его тип на плане.
#: Устройства - точками (ZLayer 70), зоны и сценарии - прямоугольниками.
PLACEMENT: dict[str, tuple[str, str, str, str]] = {
    "device": ("PointObject", "PointObjects", "GKDevice", "70"),
    "zone": ("RectangleObject", "RectangleObjects", "GKZone", "0"),
    "scenario": ("RectangleObject", "RectangleObjects", "GKDelay", "0"),
}


def _color(tag: str, argb: tuple[int, int, int, int]) -> ET.Element:
    element = ET.Element(tag)
    for name, value in zip(("A", "R", "G", "B"), argb):
        ET.SubElement(element, name).text = str(value)
    return element


def new_plan(
    name: str,
    width: float,
    height: float,
    *,
    uid: str = "",
    description: str = "",
    background: str = "",
    source_name: str = "",
) -> ET.Element:
    """Собирает <Plan> со всеми контейнерами фигур в порядке схемы."""
    plan = ET.Element("Plan")
    for field in PLAN_FIELDS:
        if field == "UID":
            plan.append(_leaf(field, uid or new_uid()))
        elif field == "No":
            plan.append(_leaf(field, "0"))
        elif field == "Name":
            plan.append(_leaf(field, name))
        elif field == "Description":
            if description:
                plan.append(_leaf(field, description))
        elif field == "BackgroundColor":
            plan.append(_color(field, (255, 255, 255, 255)))
        elif field == "BackgroundImageSource":
            plan.append(_leaf(field, background or None))
        elif field == "BackgroundSourceName":
            if source_name:
                plan.append(_leaf(field, source_name))
        elif field == "Width":
            plan.append(_leaf(field, _number(width)))
        elif field == "Height":
            plan.append(_leaf(field, _number(height)))
        elif field == "ImageType":
            plan.append(_leaf(field, "Image"))
        elif field in ("IncidentUID", "LocationUID"):
            plan.append(_leaf(field, NIL_UID))
        elif field == "IsAsynchronousLoad":
            plan.append(_leaf(field, "true"))
        elif field == "IsNotShowPlan":
            plan.append(_leaf(field, "false"))
        elif field in PLAN_CONTAINERS:
            plan.append(_leaf(field, None))
        else:
            plan.append(_leaf(field, None))
    return plan


def _number(value: float) -> str:
    """Целые пишутся без дробной части - так их пишет и Global Monitor."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def new_placement(
    kind: str,
    item_uid: str,
    label: str,
    left: float,
    top: float,
    width: float | None = None,
    height: float | None = None,
    *,
    uid: str = "",
) -> ET.Element:
    """Собирает объект на плане, ссылающийся на объект ГК."""
    if kind not in PLACEMENT:
        known = ", ".join(sorted(PLACEMENT))
        raise ValueError(f"на планах рисуются {known}, а не {kind}")
    tag, _, object_name, layer = PLACEMENT[kind]
    fields = POINT_FIELDS if tag == "PointObject" else RECTANGLE_FIELDS

    element = ET.Element(tag)
    for field in fields:
        if field == "UID":
            element.append(_leaf(field, uid or new_uid()))
        elif field == "No":
            element.append(_leaf(field, "0"))
        elif field == "Name":
            element.append(_leaf(field, label))
        elif field == "BorderColor":
            element.append(_color(field, (255, 0, 0, 0)))
        elif field == "BackgroundColor":
            element.append(_color(field, (255, 255, 255, 255)))
        elif field == "BorderThickness":
            element.append(_leaf(field, "1"))
        elif field == "ImageType":
            element.append(_leaf(field, "Image"))
        elif field in ("ZIndex",):
            element.append(_leaf(field, "0"))
        elif field == "IsLocked":
            element.append(_leaf(field, "false"))
        elif field == "IsEnabled":
            element.append(_leaf(field, "true"))
        elif field == "IsHidden":
            element.append(_leaf(field, "false"))
        elif field == "ZLayer":
            element.append(_leaf(field, layer))
        elif field == "Left":
            element.append(_leaf(field, _number(left)))
        elif field == "Top":
            element.append(_leaf(field, _number(top)))
        elif field == "Width":
            element.append(_leaf(field, _number(width if width is not None else 100)))
        elif field == "Height":
            element.append(_leaf(field, _number(height if height is not None else 100)))
        elif field == "ItemUID":
            element.append(_leaf(field, item_uid))
        elif field == "ModuleName":
            element.append(_leaf(field, MODULE_NAME))
        elif field == "ObjectName":
            element.append(_leaf(field, object_name))
        elif field == "DisplayStateChangeOnPlanTree":
            element.append(_leaf(field, "true"))
        else:
            element.append(_leaf(field, None))
    return element
