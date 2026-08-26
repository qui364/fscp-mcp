"""Проверка адресации на синтетической конфигурации.

Дерево в factories подобрано так, чтобы задеть все четыре правила из
FscpArchive._address_for. Сверка правила с эталоном — подписями объектов на
планах, посчитанными самим Global Monitor, — живёт в test_real_configs.py и
идёт по реальным конфигурациям.
"""

from __future__ import annotations

import pytest

from fscp_mcp import archive, paging, views

from . import factories


def test_известный_адрес_и_путь(synthetic):
    device = synthetic.device(factories.IP_ADDRESS)
    assert device.name == factories.IP_NAME
    assert device.driver.short == factories.IP_DRIVER
    assert [d.driver.short for d in views.ancestry(device)] == [
        "ИП 212-149",
        "АЛС",
        "КАУ",
        "РСГК",
        "Локальная сеть",
    ]


def test_описание_дописывается_к_имени_без_пробела(synthetic):
    assert synthetic.device(factories.DESCRIBED_ADDRESS).name == factories.DESCRIBED_NAME


def test_дети_группового_устройства_на_одном_уровне_с_ним(synthetic):
    group = synthetic.device(factories.GROUP_ADDRESS)
    assert group.driver.is_group
    assert factories.GROUP_CHILD_ADDRESS in [c.address for c in group.children]


def test_контейнер_без_адреса_показывает_адрес_родителя(synthetic):
    relays = synthetic.devices_by_uid[factories.D_RELAYS]
    assert relays.driver.no_address
    assert relays.address == factories.RELAYS_ADDRESS
    assert [c.address for c in relays.children] == [factories.RELAY_CHILD_ADDRESS]


def test_стволы_нумеруются_по_порядку(synthetic):
    assert synthetic.device(factories.SECOND_TRUNK_ADDRESS).name.endswith(
        factories.SECOND_TRUNK_ADDRESS
    )


def test_поиск_по_uid_и_по_адресу_дают_одно(synthetic):
    by_address = synthetic.device(factories.IP_ADDRESS)
    by_uid = synthetic.device(by_address.uid)
    assert by_uid is by_address


def test_гк_показывается_по_ip(synthetic):
    assert len(synthetic.gk_devices) == factories.EXPECTED_GK
    for gk in synthetic.gk_devices:
        assert gk.name.startswith("ГК ")
        assert gk.property_value("IPAddress") in factories.GK_IPS


def test_неизвестный_адрес(synthetic):
    with pytest.raises(archive.FscpError, match="не найдено устройство"):
        synthetic.device("99.99.99")


def test_адрес_согласован_с_родителем(synthetic):
    """Внутренняя согласованность: адрес ребёнка продолжает адрес родителя."""
    for device in synthetic.devices_by_uid.values():
        parent = device.parent
        if parent is None or not device.address or not parent.address:
            continue
        if device.driver.no_address:
            assert device.address == parent.address
        elif parent.driver.is_group or parent.driver.no_address:
            assert device.address.rpartition(".")[0] == parent.address.rpartition(".")[0]
        else:
            assert device.address == f"{parent.address}.{device.int_address}"


def test_зона_знает_свои_устройства(synthetic):
    devices = views.devices_in_zone(synthetic, factories.ZONE1)
    assert factories.IP_ADDRESS in [d.address for d in devices]


def test_страницы_не_превышают_потолок():
    page = paging.page([{"n": n} for n in range(paging.MAX_LIMIT * 2)], offset=0, limit=10_000)
    assert page["returned"] == paging.MAX_LIMIT
    assert page["total"] == paging.MAX_LIMIT * 2
    assert page["next_offset"] == paging.MAX_LIMIT


def test_дерево_обрезается_по_потолку(synthetic):
    text = views.tree_text(synthetic.root_device, max_depth=99, max_nodes=5)
    assert len(text.splitlines()) <= 6
    assert "обрезано" in text
