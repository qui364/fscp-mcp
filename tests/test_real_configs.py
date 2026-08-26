"""Проверки, которые имеют смысл только на реальных конфигурациях.

Эталон адресации — поле Name объектов на планах: там тот же адрес уже посчитан
самим Global Monitor, так что рабочая конфигурация служит готовым набором из
тысяч кейсов. Сами конфигурации не публикуются (в них данные объекта и
SecurityConfiguration.xml с хешами паролей), поэтому каталог с ними задаётся
переменной окружения FSCP_TEST_CONFIGS. Без неё файл целиком пропускается.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
import zipfile

import pytest

from fscp_mcp import archive, views, writer

from .conftest import CONFIGS_ENV, openable_configs, real_configs
from .test_tools import call

CONFIGS = openable_configs()

pytestmark = pytest.mark.skipif(
    not CONFIGS, reason=f"нет конфигураций: задайте {CONFIGS_ENV}"
)


def plan_labels(parsed: archive.FscpArchive) -> dict[str, str]:
    if parsed.plans is None:
        return {}
    return {
        archive.text(node, "ItemUID").lower(): archive.text(node, "Name")
        for node in parsed.plans.iter()
        if archive.text(node, "ObjectName") == "GKDevice" and archive.text(node, "ItemUID")
    }


def largest() -> object:
    return max(CONFIGS, key=lambda p: p.stat().st_size)


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name[:18])
def test_адреса_совпадают_с_подписями_на_планах(config):
    """Расхождения допустимы: подписи кэшируются и устаревают после
    перенумерации АЛС. Порог держим на достигнутом уровне, чтобы поймать
    регресс в самом правиле адресации."""
    _, parsed = archive.open_archive(config)
    labels = plan_labels(parsed)
    checked = [
        (label, parsed.devices_by_uid[uid].name)
        for uid, label in labels.items()
        if uid in parsed.devices_by_uid
    ]
    if not checked:
        pytest.skip("в конфигурации нет устройств на планах")
    matched = sum(1 for label, name in checked if label == name)
    assert matched / len(checked) >= 0.93, (
        f"совпало {matched}/{len(checked)} — правило адресации сломалось"
    )


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name[:18])
def test_адрес_согласован_с_родителем(config):
    _, parsed = archive.open_archive(config)
    for device in parsed.devices_by_uid.values():
        parent = device.parent
        if parent is None or not device.address or not parent.address:
            continue
        if device.driver.no_address:
            assert device.address == parent.address
        elif parent.driver.is_group or parent.driver.no_address:
            assert device.address.rpartition(".")[0] == parent.address.rpartition(".")[0]
        else:
            assert device.address == f"{parent.address}.{device.int_address}"


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name[:18])
def test_блобы_content_опознаются_по_сигнатуре(config):
    """В Content/ лежат и PNG, и JPEG — тип только по сигнатуре."""
    _, parsed = archive.open_archive(config)
    for info in parsed.images.values():
        assert info.media_type in ("image/png", "image/jpeg"), (
            f"{info.guid} оказался {info.media_type}"
        )
        assert info.width and info.height, f"не прочитались размеры {info.guid}"


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name[:18])
def test_размеры_из_заголовка_совпадают_с_фактическими(config):
    """Сверяем прочитанное из заголовка с тем, что видит полноценный декодер."""
    from io import BytesIO

    PIL = pytest.importorskip("PIL.Image", reason="нужна Pillow: pip install -e .[img]")
    _, parsed = archive.open_archive(config)
    for info in list(parsed.images.values())[:4]:
        with PIL.open(BytesIO(parsed.blob(info.guid))) as picture:
            assert (info.width, info.height) == picture.size, info.guid


def test_крупная_конфигурация_разбирается_быстро():
    """25 МБ XML должны укладываться в несколько секунд, иначе сессия бесполезна."""
    config = largest()
    if config.stat().st_size < 5_000_000:
        pytest.skip("нет крупной конфигурации")

    archive.close(archive.open_archive(config)[0])
    started = time.perf_counter()
    _, parsed = archive.open_archive(config)
    elapsed = time.perf_counter() - started
    assert elapsed < 10, f"разбор занял {elapsed:.1f}с"
    assert parsed.summary()["devices"] > 5000


def test_вложенные_планы_разбираются():
    _, parsed = archive.open_archive(largest())
    tree = views.plan_tree(parsed)
    assert len(tree) == len(parsed.plan_roots)
    assert any("children" in node for node in tree), "во вложенных планах есть дети"


def test_ресурсы_приложения_не_путаются_с_подложками():
    """GKModule/Images/Zone.png — иконка внутри Global Monitor, не запись архива."""
    _, parsed = archive.open_archive(largest())
    assert parsed.resource_refs
    for value in parsed.resource_refs:
        assert "/" in value or value.endswith(".png")
        assert value.lower() not in parsed.images
        assert views.image_reference(parsed, value)["kind"] == "app_resource"


@pytest.mark.anyio
async def test_валидатор_ловит_устаревшие_подписи():
    """Устаревшие подписи есть не в каждой конфигурации — они появляются после
    перенумерации АЛС, поэтому детектор проверяется по всему набору сразу.
    Сирот в Content/ при этом не должно быть нигде."""
    stale = 0
    for config in CONFIGS:
        opened = await call("fscp_open", path=str(config))
        report = await call("validate_config", handle=opened["handle"])
        stale += report["stale_plan_labels"]["total"]
        assert report["orphan_images"] == [], config.name
        # Проверки уровня error должны держаться на 100% рабочих конфигураций -
        # это и есть условие, по которому им разрешено блокировать fscp_save.
        # Предупреждения (dangling_ref, unknown_driver) не проверяются здесь:
        # в паре конфигураций они законно есть — сам Global Monitor их пишет.
        assert report["errors"]["total"] == 0, (config.name, report["errors"]["examples"])
    assert stale > 0


def test_переменная_окружения_указывает_на_каталог():
    """Задан каталог, а не отдельный файл — иначе набор молча схлопнется."""
    assert real_configs(), f"{CONFIGS_ENV} указывает на каталог без .fscp"


# ------------------------------------------------------------------- запись


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name[:18])
def test_каждая_запись_xml_сериализуется_побайтово(config):
    """Эталон формата задаёт сам Global Monitor - это и есть проверка.

    Разобранная и записанная обратно запись обязана совпасть с исходной до
    байта. Пока это так, любое расхождение в открытом файле - следствие правки,
    а не сериализации. SecurityConfiguration.xml исключён: его не разбираем.
    """
    with zipfile.ZipFile(config) as source:
        names = [
            n
            for n in source.namelist()
            if n.endswith(".xml") and n != archive.SECURITY_CONFIG
        ]
        assert names, f"{config.name}: в архиве нет XML"
        for name in names:
            raw = source.read(name)
            attrs, newline = writer.root_header(raw)
            again = writer.serialize(
                ET.fromstring(raw), root_attrs=attrs, newline=newline
            )
            assert again == raw, f"{config.name}/{name}"


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.name[:18])
def test_сохранение_без_правок_не_меняет_ни_одной_записи(config, tmp_path):
    """Круг «открыть - сохранить» на рабочей конфигурации.

    Проверяются и содержимое записей, и их порядок, и метаданные: порядок между
    версиями Global Monitor не стабилен, а блобы Content/ несут собственные
    таймстампы, отличные от времени сохранения конфигурации.
    """
    _, parsed = archive.open_archive(config)
    target = tmp_path / config.name
    parsed.save(target)

    with zipfile.ZipFile(config) as before, zipfile.ZipFile(target) as after:
        assert before.namelist() == after.namelist(), config.name
        for name in before.namelist():
            assert before.read(name) == after.read(name), f"{config.name}/{name}"
            old, new = before.getinfo(name), after.getinfo(name)
            assert old.date_time == new.date_time, f"{config.name}/{name}"
            assert old.compress_type == new.compress_type, f"{config.name}/{name}"
            assert old.external_attr == new.external_attr, f"{config.name}/{name}"


def test_сохранение_крупной_конфигурации_укладывается_в_бюджет(tmp_path):
    """25 МБ должны сохраняться за секунды, иначе инструментом не пользуются."""
    config = largest()
    if config.stat().st_size < 5_000_000:
        pytest.skip("нет крупной конфигурации")

    _, parsed = archive.open_archive(config)
    started = time.perf_counter()
    parsed.save(tmp_path / config.name)
    assert time.perf_counter() - started < 10
