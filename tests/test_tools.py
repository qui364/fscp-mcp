"""Проверка инструментов через реальный слой MCP, а не напрямую функции."""

from __future__ import annotations

import json

import pytest

from fscp_mcp import drivers
from fscp_mcp.server import server

from . import factories


async def call(name: str, **arguments):
    result = await server.call_tool(name, arguments)
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
