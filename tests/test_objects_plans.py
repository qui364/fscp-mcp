"""Правка объектов ГК, логики и планов.

Инварианты, снятые с рабочих конфигураций: No — устойчивый идентификатор, а не
позиция; условие без целей не остаётся после каскада; связь план↔объект
двусторонняя.
"""

from __future__ import annotations

import pytest

from fscp_mcp import archive as arch
from fscp_mcp import edits, logic, schema
from fscp_mcp.errors import FscpError

from . import factories


@pytest.fixture
def opened(tmp_path):
    path = factories.build(tmp_path / "синтетика.fscp")
    handle, parsed = arch.open_archive(path)
    yield parsed
    arch.close(handle)


# ------------------------------------------------------------- объекты ГК


def test_новый_объект_получает_следующий_номер(opened):
    """No берётся как max+1: в рабочих конфигурациях он идёт с дырами и
    сортировке не подчиняется, значит это идентификатор, а не позиция."""
    было = [int(ref.no) for ref in opened.objects_by_kind["zone"]]

    uid = edits.add_object(opened, "zone", "Подвал")

    assert int(opened.objects_by_uid[uid].no) == max(было) + 1


def test_номера_соседей_не_сдвигаются_при_удалении(opened):
    """Перенумерация переименовала бы все соседние объекты: имя показывается
    как «{No}.{Name}» и кэшируется в подписях на планах."""
    edits.add_object(opened, "zone", "Подвал")
    номера = {ref.uid: ref.no for ref in opened.objects_by_kind["zone"]}
    жертва = next(iter(номера))

    edits.remove_object(opened, жертва, force=True)

    for ref in opened.objects_by_kind["zone"]:
        assert ref.no == номера[ref.uid]


def test_поля_нового_объекта_в_каноническом_порядке(opened):
    uid = edits.add_object(opened, "scenario", "ОПОВЕЩЕНИЕ")
    tags = [c.tag for c in opened.objects_by_uid[uid].element]

    поток = iter(schema.SCENARIO_FIELDS)
    assert all(tag in поток for tag in tags), tags


def test_вид_без_снятой_схемы_создать_нельзя(opened):
    """Направления, МПТ и охранные зоны пусты во всех доступных конфигурациях:
    структуру пришлось бы выдумать, а ошибка проявилась бы только в программе."""
    with pytest.raises(FscpError, match="схема известна только"):
        edits.add_object(opened, "direction", "Направление 1")


def test_номер_объекта_менять_нельзя(opened):
    uid = opened.objects_by_kind["zone"][0].uid
    with pytest.raises(FscpError, match="ссылаются"):
        edits.set_object(opened, uid, fields={"No": "99"})


def test_имя_объекта_меняется(opened):
    ref = opened.objects_by_kind["zone"][0]
    result = edits.set_object(opened, ref.uid, name="Переименованная")

    assert result["name"].endswith("Переименованная")
    assert opened.objects_by_uid[ref.uid].name == result["name"]


# ------------------------------------------------------------------ логика


def test_условие_складывается_в_список_по_виду_целей(opened):
    """Зоны идут в ZoneUIDs с операцией AnyZone — список и название операции
    выбираются по виду целей, а не задаются вызывающим."""
    scenario = edits.add_object(opened, "scenario", "ТЕСТ")
    zones = [ref.uid for ref in opened.objects_by_kind["zone"]]

    result = edits.add_clause(opened, scenario, targets=zones, state="Пожар2")

    clause = opened.objects_by_uid[scenario].element.find(
        "Logic/OnClausesGroup/Clauses/GKClause"
    )
    assert clause.findtext("ClauseOperationType") == "AnyZone"
    assert len(clause.find("ZoneUIDs")) == len(zones)
    assert len(clause.find("DeviceUIDs")) == 0
    assert "Пожар2" in result["logic"]["Включение"]


def test_условие_на_всех_целях_меняет_операцию(opened):
    scenario = edits.add_object(opened, "scenario", "ТЕСТ")
    zones = [ref.uid for ref in opened.objects_by_kind["zone"]]

    edits.add_clause(opened, scenario, targets=zones, every=True)

    clause = opened.objects_by_uid[scenario].element.find(
        "Logic/OnClausesGroup/Clauses/GKClause"
    )
    assert clause.findtext("ClauseOperationType") == "AllZones"


def test_смешивать_виды_целей_нельзя(opened):
    scenario = edits.add_object(opened, "scenario", "ТЕСТ")
    zone = opened.objects_by_kind["zone"][0].uid
    device = opened.devices_by_address[factories.IP_ADDRESS].uid

    with pytest.raises(FscpError, match="одного вида"):
        edits.add_clause(opened, scenario, targets=[zone, device])


def test_условие_без_целей_отвергается(opened):
    scenario = edits.add_object(opened, "scenario", "ТЕСТ")
    with pytest.raises(FscpError, match="без целей"):
        edits.add_clause(opened, scenario, targets=[])


def test_состояние_принимается_и_по_русски(opened):
    scenario = edits.add_object(opened, "scenario", "ТЕСТ")
    zone = opened.objects_by_kind["zone"][0].uid

    edits.add_clause(opened, scenario, targets=[zone], state="Пожар1")

    clause = opened.objects_by_uid[scenario].element.find(
        "Logic/OnClausesGroup/Clauses/GKClause"
    )
    assert clause.findtext("StateType") == "Fire1"


def test_группы_условий_идут_до_счётчика(opened):
    """В <Logic> группы стоят перед UseOffCounterLogic — порядок снят с рабочих
    конфигураций, а XmlSerializer на чужом молча теряет поле."""
    scenario = edits.add_object(opened, "scenario", "ТЕСТ")
    zone = opened.objects_by_kind["zone"][0].uid

    edits.add_clause(opened, scenario, targets=[zone], group="OffClausesGroup")
    edits.add_clause(opened, scenario, targets=[zone], group="OnClausesGroup")

    block = opened.objects_by_uid[scenario].element.find("Logic")
    tags = [c.tag for c in block]
    поток = iter(schema.LOGIC_ORDER)
    assert all(tag in поток for tag in tags), tags
    assert tags.index("OnClausesGroup") < tags.index("UseOffCounterLogic")


def test_каскад_убирает_условие_оставшееся_без_целей(opened):
    """Условие без целей — самое вероятное из того, обо что споткнётся
    Global Monitor: смысла у него нет, и в рабочих файлах таких нет."""
    zone = edits.add_object(opened, "zone", "Одиночная")
    scenario = edits.add_object(opened, "scenario", "ТЕСТ")
    edits.add_clause(opened, scenario, targets=[zone])

    result = edits.remove_object(opened, zone, force=True)

    assert result["clauses_pruned"] == 1
    assert logic.render(opened, opened.objects_by_uid[scenario].element) == {}


def test_логика_убирается_целиком(opened):
    scenario = edits.add_object(opened, "scenario", "ТЕСТ")
    zone = opened.objects_by_kind["zone"][0].uid
    edits.add_clause(opened, scenario, targets=[zone])

    edits.clear_logic(opened, scenario)

    assert logic.render(opened, opened.objects_by_uid[scenario].element) == {}


# ------------------------------------------------------------------- планы


def test_план_создаётся_со_всеми_контейнерами(opened):
    uid = edits.add_plan(opened, "Этаж 2")
    tags = [c.tag for c in opened.plans_by_uid[uid]]

    поток = iter(schema.PLAN_FIELDS)
    assert all(tag in поток for tag in tags), tags
    for container in schema.PLAN_CONTAINERS:
        assert opened.plans_by_uid[uid].find(container) is not None, container


def test_подложка_кладётся_в_архив_и_задаёт_размеры(opened, tmp_path):
    """Размеры берутся из заголовка картинки: оригиналы бывают 5000x4749, и
    подставлять A4 к такой подложке бессмысленно."""
    image = tmp_path / "этаж.png"
    image.write_bytes(factories.png(120, 80))

    uid = edits.add_plan(opened, "Этаж 2", image_path=str(image))

    plan = opened.plans_by_uid[uid]
    guid = plan.findtext("BackgroundImageSource")
    assert f"{arch.CONTENT_PREFIX}{guid}" in opened.added_entries
    assert plan.findtext("Width") == "120"
    assert plan.findtext("Height") == "80"
    assert opened.blob(guid) == image.read_bytes()


def test_не_картинка_подложкой_не_становится(opened, tmp_path):
    junk = tmp_path / "документ.pdf"
    junk.write_bytes(b"%PDF-1.4 not an image")

    with pytest.raises(FscpError, match="PNG и JPEG"):
        edits.add_plan(opened, "Этаж 2", image_path=str(junk))


def test_вложенный_план_попадает_в_children(opened):
    parent = edits.add_plan(opened, "Этаж 2")
    child = edits.add_plan(opened, "Щитовая", parent_uid=parent)

    assert opened.plan_parent[child] == parent
    assert child in opened.plan_children[parent]


def test_связь_с_планом_ставится_с_обеих_сторон(opened):
    """ItemUID смотрит на объект ГК, PlanElementUIDs — обратно на объект плана.
    Односторонняя связь и есть тот висячий GUID, который потом не объяснить."""
    plan = edits.add_plan(opened, "Этаж 2")
    device = opened.devices_by_address[factories.IP_ADDRESS]

    result = edits.place_object(opened, plan, device.uid, left=10, top=20)

    node = next(
        n for uid, n in opened.plan_objects_by_plan[plan] if uid == device.uid
    )
    assert node.findtext("ItemUID") == device.uid
    assert node.findtext("ObjectName") == "GKDevice"
    assert node.findtext("Name") == device.name

    links = [
        g.text
        for g in opened.devices_by_uid[device.uid]
        .element.find("PlanElementUIDs")
        .findall("guid")
    ]
    assert result["uid"] in links


def test_зона_рисуется_прямоугольником(opened):
    """Устройства — точками, зоны и сценарии — прямоугольниками: так делает
    сам Global Monitor во всех просмотренных конфигурациях."""
    plan = edits.add_plan(opened, "Этаж 2")
    zone = opened.objects_by_kind["zone"][0]

    result = edits.place_object(
        opened, plan, zone.uid, left=5, top=5, width=200, height=100
    )

    assert result["element"] == "RectangleObject"
    node = next(n for uid, n in opened.plan_objects_by_plan[plan] if uid == zone.uid)
    assert node.findtext("ObjectName") == "GKZone"
    assert node.findtext("Width") == "200"


def test_снятие_с_плана_убирает_обе_стороны(opened):
    plan = edits.add_plan(opened, "Этаж 2")
    device = opened.devices_by_address[factories.IP_ADDRESS]
    placed = edits.place_object(opened, plan, device.uid)

    edits.remove_placement(opened, plan, device.uid)

    assert not [
        n for uid, n in opened.plan_objects_by_plan.get(plan, []) if uid == device.uid
    ]
    links = [
        g.text
        for g in opened.devices_by_uid[device.uid]
        .element.find("PlanElementUIDs")
        .findall("guid")
    ]
    assert placed["uid"] not in links


def test_удаление_плана_с_объектами_требует_force(opened):
    plan = edits.add_plan(opened, "Этаж 2")
    device = opened.devices_by_address[factories.IP_ADDRESS]
    edits.place_object(opened, plan, device.uid)

    with pytest.raises(FscpError, match="force=true"):
        edits.remove_plan(opened, plan)

    result = edits.remove_plan(opened, plan, force=True)
    assert result["objects_removed"] == 1
    assert result["links_removed"] == 1
    assert plan not in opened.plans_by_uid


# ------------------------------------------------------- круг с сохранением


def test_всё_переживает_сохранение_и_открытие(opened, tmp_path):
    zone = edits.add_object(opened, "zone", "Подвал")
    scenario = edits.add_object(opened, "scenario", "ОПОВЕЩЕНИЕ")
    edits.add_clause(opened, scenario, targets=[zone], state="Пожар2")
    image = tmp_path / "этаж.png"
    image.write_bytes(factories.png(64, 48))
    plan = edits.add_plan(opened, "Этаж 2", image_path=str(image))
    device = opened.devices_by_address[factories.IP_ADDRESS]
    edits.place_object(opened, plan, device.uid, left=7, top=9)

    target = tmp_path / "полный.fscp"
    opened.save(target)

    handle, reopened = arch.open_archive(target)
    try:
        assert reopened.objects_by_uid[zone].name.endswith("Подвал")
        assert "Пожар2" in logic.render(
            reopened, reopened.objects_by_uid[scenario].element
        )["Включение"]
        assert reopened.plans_by_uid[plan].findtext("Name") == "Этаж 2"
        guid = reopened.plans_by_uid[plan].findtext("BackgroundImageSource")
        assert reopened.blob(guid) == image.read_bytes()
        assert [
            uid for uid, _ in reopened.plan_objects_by_plan[plan]
        ] == [device.uid]
    finally:
        arch.close(handle)
