"""Проверка инструментов через реальный слой MCP, а не напрямую функции."""

from __future__ import annotations

import json

import pytest

from fscp_mcp import drivers
from fscp_mcp.server import server

from . import factories


async def call(tool: str, **arguments):
    """tool, не name: несколько инструментов (add_object, add_plan, ...) сами
    принимают параметр name, и он не должен путаться с именем инструмента."""
    result = await server.call_tool(tool, arguments)
    blocks = result.content if hasattr(result, "content") else result
    if isinstance(blocks, tuple):
        blocks = blocks[0]
    if not isinstance(blocks, list):
        blocks = [blocks]
    payload = "".join(getattr(b, "text", "") for b in blocks)
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return payload


@pytest.mark.anyio
async def test_открытие_и_сводка(handle):
    info = await call("fscp_info", handle=handle)
    assert info["devices"] == factories.EXPECTED_DEVICES
    assert len(info["gk"]) == factories.EXPECTED_GK
    assert "SecurityConfiguration.xml" in info["unparsed"]


@pytest.mark.anyio
async def test_карточка_устройства(handle):
    device = await call("get_device", handle=handle, device=factories.IP_ADDRESS)
    assert device["name"] == factories.IP_NAME
    assert device["driver"]["short"] == factories.IP_DRIVER
    assert len(device["zones"]) == factories.EXPECTED_ZONES
    name, value = factories.IP_PROPERTY
    assert device["properties"][name] == value


@pytest.mark.anyio
async def test_ошибки_возвращаются_текстом_без_трейсбека(handle, tmp_path):
    empty = factories.build_empty(tmp_path / "пустой.fscp")
    for bad in (
        await call("fscp_info", handle="нет-такого"),
        await call("get_device", handle=handle, device="99.99"),
        await call("fscp_open", path=str(empty)),
    ):
        assert "error" in bad
        assert "Traceback" not in json.dumps(bad, ensure_ascii=False)


@pytest.mark.anyio
async def test_поиск_требует_критерия(handle):
    assert "error" in await call("search_devices", handle=handle)


@pytest.mark.anyio
async def test_поиск_по_типу_и_адресу(handle):
    hits = await call("search_devices", handle=handle, driver="ИП 212")
    assert hits["total"] >= 2
    assert all(h["driver"].startswith("ИП 212") for h in hits["devices"])

    prefix = factories.IP_ADDRESS.rpartition(".")[0]
    prefixed = await call("search_devices", handle=handle, address_prefix=prefix)
    assert prefixed["total"] >= 2
    assert all(h["address"].startswith(prefix) for h in prefixed["devices"])


@pytest.mark.anyio
async def test_поиск_по_описанию(handle):
    hits = await call("search_devices", handle=handle, description="Холл")
    assert [h["name"] for h in hits["devices"]] == [factories.DESCRIBED_NAME]


@pytest.mark.anyio
async def test_логика_разворачивается_в_текст(handle):
    scenarios = await call("list_objects", handle=handle, kind="scenario")
    detail = await call("get_object", handle=handle, uid=scenarios["objects"][0]["uid"])
    assert detail["logic"]["Включение"] == factories.SCENARIO_LOGIC
    assert detail["direction_devices"]


@pytest.mark.anyio
async def test_resolve_uid_различает_виды(handle):
    device = await call("get_device", handle=handle, device=factories.IP_ADDRESS)
    assert (await call("resolve_uid", handle=handle, uid=device["uid"]))["kind"] == "device"
    assert (await call("resolve_uid", handle=handle, uid=device["zones"][0]["uid"]))["kind"] == "zone"
    assert (await call("resolve_uid", handle=handle, uid=drivers.GK))["kind"] == "driver"
    assert (await call("resolve_uid", handle=handle, uid="deadbeef-0000-0000-0000-000000000000"))[
        "kind"
    ] == "unknown"


@pytest.mark.anyio
async def test_неизвестный_вид_объекта_подсказывает_доступные(handle):
    bad = await call("list_objects", handle=handle, kind="вертолёт")
    assert "error" in bad and "zone" in bad["error"]


@pytest.mark.anyio
async def test_read_xml_не_пускает_в_security(handle):
    bad = await call("read_xml", handle=handle, path="Users", config="SecurityConfiguration.xml")
    assert "error" in bad and "намеренно" in bad["error"]


@pytest.mark.anyio
async def test_read_xml_обрезает_вывод(handle):
    fragment = await call("read_xml", handle=handle, path="RootDevice", max_chars=200)
    assert fragment["truncated"] is True
    assert len(fragment["xml"]) <= 200


@pytest.mark.anyio
async def test_экспорт_csv(handle, tmp_path):
    target = tmp_path / "devices.csv"
    result = await call("export_devices_csv", handle=handle, out_path=str(target))
    assert result["rows"] == factories.EXPECTED_DEVICES
    body = target.read_text(encoding="utf-8-sig")
    assert body.startswith("address;name;driver")
    assert factories.IP_NAME in body


@pytest.mark.anyio
async def test_планы_перечисляются(handle):
    plans = await call("list_plans", handle=handle)
    assert plans["total"] == 2
    detail = await call("get_plan", handle=handle, plan_uid=factories.PLAN_ROOT)
    assert detail["name"] == factories.PLAN_NAME


@pytest.mark.anyio
async def test_подложка_выгружается_на_диск(handle, tmp_path):
    listing = await call("list_plan_images", handle=handle)
    assert listing["total"] == 2
    assert listing["orphans"] == []
    assert listing["app_resources"] == [factories.APP_RESOURCE]

    result = await call(
        "extract_plan_image", handle=handle, guid=factories.PNG_GUID, out_path=str(tmp_path)
    )
    written = tmp_path / f"{factories.PNG_GUID}.png"
    assert written.exists()
    assert written.stat().st_size == result["size_bytes"]
    assert written.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.anyio
async def test_валидатор_ловит_устаревшую_подпись(handle):
    report = await call("validate_config", handle=handle)
    stale = report["stale_plan_labels"]
    assert stale["total"] == 1
    assert stale["examples"][0]["label_on_plan"] == factories.STALE_LABEL
    assert stale["examples"][0]["actual"] == factories.DESCRIBED_NAME
    assert report["orphan_images"] == []
    assert report["dangling_plan_objects"]["total"] == 0


@pytest.mark.anyio
async def test_справочник_драйверов_фильтруется():
    all_drivers = await call("list_drivers", limit=500)
    assert all_drivers["total"] == 102
    filtered = await call("list_drivers", query="извещатель")
    assert 0 < filtered["total"] < all_drivers["total"]


# ------------------------------------------------------------------ запись
#
# Инструменты проверяются через server.call_tool, а не вызовом функций: иначе
# не проверяются ни схема аргументов, ни превращение FscpError в {"error": ...}.
#
# handle из conftest кэширован на всю тестовую сессию (open_archive
# дедуплицирует по (path, mtime)) — мутация в одном тесте протекла бы в
# следующий. Тесты записи поэтому открывают свой файл на каждый тест и сами
# его закрывают.


@pytest.fixture
async def write_handle(tmp_path):
    path = factories.build(tmp_path / "синтетика.fscp")
    opened = await call("fscp_open", path=str(path))
    yield opened["handle"], path
    await call("fscp_close", handle=opened["handle"])


@pytest.mark.anyio
async def test_правка_копится_в_памяти_и_видна_в_diff(write_handle):
    handle, _ = write_handle
    before = await call("fscp_diff", handle=handle)
    assert before["total"] == 0
    assert before["dirty_entries"] == []

    await call(
        "add_device",
        handle=handle,
        parent=factories.LINE_ADDRESS,
        driver=factories.IP_DRIVER,
    )

    after = await call("fscp_diff", handle=handle)
    assert after["total"] == 1
    assert after["edits"][0]["what"] == "Устройство добавлено"
    assert after["dirty_entries"] == ["GKDeviceConfiguration.xml"]

    await call("fscp_revert", handle=handle)
    assert (await call("fscp_diff", handle=handle))["total"] == 0


@pytest.mark.anyio
async def test_сохранение_не_трогает_исходник(write_handle, tmp_path):
    handle, source = write_handle
    было = source.read_bytes()

    await call(
        "add_device",
        handle=handle,
        parent=factories.LINE_ADDRESS,
        driver=factories.IP_DRIVER,
    )
    result = await call(
        "fscp_save", handle=handle, out_path=str(tmp_path / "копия.fscp")
    )

    assert result["rewritten"] == ["GKDeviceConfiguration.xml"]
    assert source.read_bytes() == было


@pytest.mark.anyio
async def test_сохранение_в_исходник_отвергается(write_handle):
    handle, source = write_handle
    result = await call("fscp_save", handle=handle, out_path=str(source))
    assert "перезапись исходного файла" in result["error"]


@pytest.mark.anyio
async def test_занятый_адрес_отдаётся_текстом_а_не_трейсбеком(write_handle):
    handle, _ = write_handle
    result = await call(
        "add_device",
        handle=handle,
        parent=factories.LINE_ADDRESS,
        driver=factories.IP_DRIVER,
        int_address=1,
    )
    assert "уже есть" in result["error"]
    assert "Traceback" not in result["error"]


@pytest.mark.anyio
async def test_удаление_без_force_перечисляет_ссылки(write_handle):
    handle, _ = write_handle
    result = await call("remove_device", handle=handle, device=factories.IP_ADDRESS)
    assert "ссылаются" in result["error"]
    assert "force=true" in result["error"]


@pytest.mark.anyio
async def test_логика_собирается_и_читается(write_handle):
    handle, _ = write_handle
    scenario = await call(
        "add_object", handle=handle, kind="scenario", name="ОПОВЕЩЕНИЕ"
    )
    zones = await call("list_objects", handle=handle, kind="zone")

    result = await call(
        "add_clause",
        handle=handle,
        owner=scenario["uid"],
        targets=[zones["objects"][0]["uid"]],
        state="Пожар2",
    )

    assert "Пожар2" in result["logic"]["Включение"]


@pytest.mark.anyio
async def test_вид_объекта_без_схемы_отказывает_понятно(write_handle):
    handle, _ = write_handle
    result = await call(
        "add_object", handle=handle, kind="direction", name="Направление"
    )
    assert "схема известна только" in result["error"]


@pytest.mark.anyio
async def test_объект_наносится_на_план_и_снимается(write_handle):
    handle, _ = write_handle
    plan = await call("add_plan", handle=handle, name="Этаж 2")
    device = await call("get_device", handle=handle, device=factories.IP_ADDRESS)

    placed = await call(
        "place_object",
        handle=handle,
        plan_uid=plan["uid"],
        item=device["uid"],
        left=10,
        top=20,
    )
    assert placed["element"] == "PointObject"

    where = await call("find_on_plans", handle=handle, uid=device["uid"])
    assert any(p["plan"] == "Этаж 2" for p in where["placements"])

    await call(
        "remove_placement", handle=handle, plan_uid=plan["uid"], item=device["uid"]
    )
    where = await call("find_on_plans", handle=handle, uid=device["uid"])
    assert not any(p["plan"] == "Этаж 2" for p in where["placements"])


@pytest.mark.anyio
async def test_создание_с_нуля_даёт_рабочий_handle(tmp_path):
    created = await call("fscp_create", path=str(tmp_path / "новый.fscp"))

    assert created["devices"] == 2
    assert created["versions"]["GKDeviceConfiguration.xml"] == "2.9"
    assert "SecurityConfiguration.xml" not in created["entries"]

    tree = await call("device_tree", handle=created["handle"])
    assert "Локальная сеть" in tree["tree"]
    await call("fscp_close", handle=created["handle"])


@pytest.mark.anyio
async def test_созданный_файл_не_затирается_без_разрешения(tmp_path):
    target = tmp_path / "новый.fscp"
    first = await call("fscp_create", path=str(target))
    await call("fscp_close", handle=first["handle"])

    again = await call("fscp_create", path=str(target))
    assert "уже существует" in again["error"]


@pytest.mark.anyio
async def test_валидатор_докладывает_о_целостности(handle):
    report = await call("validate_config", handle=handle)
    assert report["errors"]["total"] == 0, report["errors"]["examples"]
