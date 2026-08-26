"""Мутации дерева устройств.

Проверяются не только результаты, но и инварианты, снятые с рабочих
конфигураций: уникальна пара (DriverUID, IntAddress), а не сам адрес; дети
идут по возрастанию адреса; No у устройств всегда 0; подписи объектов на
планах после правки не устаревают.
"""

from __future__ import annotations

import pytest

from fscp_mcp import archive as arch
from fscp_mcp import edits
from fscp_mcp.errors import FscpError

from . import factories


@pytest.fixture
def opened(tmp_path):
    """Свой архив на каждый тест: мутации портят состояние сессии.

    Закрывать обязательно: сессия с несохранёнными правками не вытесняется из
    кэша, и уже четвёртый тест упёрся бы в MAX_SESSIONS.
    """
    path = factories.build(tmp_path / "синтетика.fscp")
    handle, parsed = arch.open_archive(path)
    yield parsed
    arch.close(handle)


def addressable(device):
    return [c for c in device.children if not c.driver.no_address]


# ------------------------------------------------------------- добавление


def test_добавленное_устройство_получает_свободный_адрес(opened):
    line = opened.devices_by_address[factories.LINE_ADDRESS]
    занято = edits.line_addresses(opened, line)

    (uid,) = edits.add_device(opened, factories.LINE_ADDRESS, factories.IP_DRIVER)

    created = opened.devices_by_uid[uid]
    assert created.int_address not in занято
    assert created.address == f"{factories.LINE_ADDRESS}.{created.int_address}"


def test_адрес_ищется_по_всей_линии_а_не_среди_братьев(opened):
    """Дети сквозного узла стоят на одном уровне с ним и тоже занимают адреса.

    В синтетике АМ4 держит ребёнка на GROUP_CHILD_ADDRESS, и наивный поиск
    «первый свободный IntAddress среди братьев» выдал бы занятый адрес.
    """
    line = opened.devices_by_address[factories.LINE_ADDRESS]
    занято = edits.line_addresses(opened, line)
    хвост = int(factories.GROUP_CHILD_ADDRESS.rsplit(".", 1)[1])

    assert хвост in занято, "адрес ребёнка сквозного узла должен считаться занятым"

    (uid,) = edits.add_device(opened, factories.LINE_ADDRESS, factories.IP_DRIVER)
    assert opened.devices_by_uid[uid].int_address != хвост


def test_дети_остаются_по_возрастанию_адреса(opened):
    edits.add_device(opened, factories.LINE_ADDRESS, factories.IP_DRIVER, count=3)

    line = opened.devices_by_address[factories.LINE_ADDRESS]
    адреса = [c.int_address for c in addressable(line)]
    assert адреса == sorted(адреса), адреса


def test_у_нового_устройства_поля_в_каноническом_порядке(opened):
    from fscp_mcp import schema

    (uid,) = edits.add_device(opened, factories.LINE_ADDRESS, factories.IP_DRIVER)
    tags = [c.tag for c in opened.devices_by_uid[uid].element]

    поток = iter(schema.DEVICE_FIELDS)
    assert all(tag in поток for tag in tags), tags
    assert opened.devices_by_uid[uid].element.findtext("No") == "0"


def test_занятый_адрес_отвергается(opened):
    line = opened.devices_by_address[factories.LINE_ADDRESS]
    занятый = addressable(line)[0].int_address

    with pytest.raises(FscpError, match="уже есть"):
        edits.add_device(
            opened, factories.LINE_ADDRESS, factories.IP_DRIVER, int_address=занятый
        )


def test_одинаковый_адрес_у_разных_типов_разрешён(opened):
    """Уникальна пара (DriverUID, IntAddress), а не адрес сам по себе.

    В рабочих конфигурациях у БМП рядом стоят «Линия БМП», БМПК и БМПП с одним
    адресом — наивное правило запретило бы законную конфигурацию.
    """
    line = opened.devices_by_address[factories.LINE_ADDRESS]
    чужой = next(
        c.int_address for c in addressable(line) if c.driver.short != factories.IP_DRIVER
    )

    (uid,) = edits.add_device(
        opened, factories.LINE_ADDRESS, factories.IP_DRIVER, int_address=чужой
    )
    assert opened.devices_by_uid[uid].int_address == чужой


def test_неизвестный_драйвер_отвергается_с_подсказкой(opened):
    with pytest.raises(FscpError, match="не найден"):
        edits.add_device(opened, factories.LINE_ADDRESS, "такого прибора нет")


# ------------------------------------------------------------------ правка


def test_описание_меняет_имя_и_подпись_на_плане(opened):
    """Подпись на плане — кэш имени; не обновив её, мы сами создаём дефект,
    который ловит validate_config как «устаревшую подпись»."""
    before = opened.devices_by_address[factories.IP_ADDRESS].name

    result = edits.set_device(opened, factories.IP_ADDRESS, description="Серверная")

    assert result["name"] != before
    assert result["name"].endswith("(Серверная)")
    assert result["plan_labels_updated"] >= 1

    uid = opened.devices_by_address[factories.IP_ADDRESS].uid
    подписи = {
        node.findtext("Name") for _, node in opened.plan_objects_by_item.get(uid, [])
    }
    assert подписи == {result["name"]}
    assert arch.PLANS_CONFIG in opened.dirty


def test_пустое_описание_убирает_поле_целиком(opened):
    """XmlSerializer не пишет <Description /> — он опускает поле."""
    edits.set_device(opened, factories.DESCRIBED_ADDRESS, description="")

    element = opened.devices_by_address[factories.DESCRIBED_ADDRESS].element
    assert element.find("Description") is None


def test_смена_адреса_сохраняет_порядок_детей(opened):
    line = opened.devices_by_address[factories.LINE_ADDRESS]
    свободный = max(edits.line_addresses(opened, line)) + 5

    edits.set_device(opened, factories.IP_ADDRESS, int_address=свободный)

    line = opened.devices_by_address[factories.LINE_ADDRESS]
    адреса = [c.int_address for c in addressable(line)]
    assert адреса == sorted(адреса), адреса


def test_правка_без_полей_отвергается(opened):
    with pytest.raises(FscpError, match="ни одного поля"):
        edits.set_device(opened, factories.IP_ADDRESS)


def test_свойство_прибора_переписывается(opened):
    имя, старое = factories.IP_PROPERTY
    edits.set_device(opened, factories.IP_ADDRESS, properties={имя: "70"})

    device = opened.devices_by_address[factories.IP_ADDRESS]
    assert device.property_value(имя) == "70" != старое


# ----------------------------------------------------------------- перенос


def test_перенос_меняет_адрес_и_родителя(opened):
    result = edits.move_device(
        opened, factories.GROUP_ADDRESS, factories.SECOND_LINE_ADDRESS
    )

    assert result["was"] == factories.GROUP_ADDRESS
    assert result["address"].startswith(factories.SECOND_LINE_ADDRESS)
    moved = opened.devices_by_uid[result["uid"]]
    assert moved.parent.address == factories.SECOND_LINE_ADDRESS


def test_перенос_внутрь_себя_отвергается(opened):
    """Иначе поддерево отцепилось бы от дерева и молча исчезло."""
    with pytest.raises(FscpError, match="петлю"):
        edits.move_device(
            opened, factories.GROUP_ADDRESS, factories.GROUP_CHILD_ADDRESS
        )


def test_корень_не_переносится(opened):
    with pytest.raises(FscpError, match="[Кк]орень"):
        edits.move_device(opened, opened.root_device.uid, factories.LINE_ADDRESS)


# ---------------------------------------------------------------- удаление


def test_удаление_отказывает_пока_на_устройство_ссылаются(opened):
    with pytest.raises(FscpError, match="ссылаются"):
        edits.remove_device(opened, factories.IP_ADDRESS)

    assert factories.IP_ADDRESS in opened.devices_by_address, "ничего не удалено"


def test_удаление_с_force_вычищает_ссылки(opened):
    uid = opened.devices_by_address[factories.IP_ADDRESS].uid
    было = len(opened.referrers(uid))
    assert было, "устройство должно быть на плане, иначе тест бессмысленный"

    result = edits.remove_device(opened, factories.IP_ADDRESS, force=True)

    assert result["references_cleaned"] == было
    assert uid not in opened.devices_by_uid
    assert opened.plan_objects_by_item.get(uid) in (None, [])


def test_удаляется_всё_поддерево(opened):
    группа = opened.devices_by_address[factories.GROUP_ADDRESS]
    ожидалось = len(edits.subtree_uids(группа))
    assert ожидалось > 1, "у сквозного узла должны быть дети"

    result = edits.remove_device(opened, factories.GROUP_ADDRESS, force=True)
    assert result["removed"] == ожидалось


def test_корень_не_удаляется(opened):
    with pytest.raises(FscpError, match="[Кк]орень"):
        edits.remove_device(opened, opened.root_device.uid, force=True)


# ------------------------------------------------------------------ журнал


def test_журнал_пополняется_и_помечает_записи(opened):
    edits.add_device(opened, factories.LINE_ADDRESS, factories.IP_DRIVER)
    edits.set_device(opened, factories.IP_ADDRESS, description="Холл")

    assert [e.op for e in opened.journal] == ["add", "set"]
    assert [e.seq for e in opened.journal] == [1, 2]
    assert arch.GK_CONFIG in opened.dirty


def test_откат_возвращает_исходные_байты(opened, tmp_path):
    """Сильнейшее утверждение об откате: сериализация совпадает с исходной."""
    from fscp_mcp import writer

    было = writer.serialize(
        opened.gk, root_attrs=opened.headers[arch.GK_CONFIG][0], newline="\n"
    )

    edits.add_device(opened, factories.LINE_ADDRESS, factories.IP_DRIVER, count=3)
    edits.set_device(opened, factories.IP_ADDRESS, description="Холл")
    assert opened.dirty

    opened.revert()

    стало = writer.serialize(
        opened.gk, root_attrs=opened.headers[arch.GK_CONFIG][0], newline="\n"
    )
    assert стало == было
    assert opened.journal == []
    assert opened.dirty == set()


def test_правка_переживает_сохранение_и_открытие(opened, tmp_path):
    (uid,) = edits.add_device(
        opened, factories.LINE_ADDRESS, factories.IP_DRIVER, description="Круг"
    )
    ожидалось = opened.devices_by_uid[uid].name

    target = tmp_path / "с-правкой.fscp"
    opened.save(target)

    _, reopened = arch.open_archive(target)
    assert reopened.devices_by_uid[uid].name == ожидалось
    assert len(reopened.devices_by_uid) == factories.EXPECTED_DEVICES + 1
