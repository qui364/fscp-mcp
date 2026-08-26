"""Создание конфигурации с нуля.

Шаблон в src/fscp_mcp/skeleton — это снятая с рабочей конфигурации байтовая
раскладка, а не сочинённая структура. Здесь проверяется, что она обезличена,
что созданный файл открывается нашим же разбором и что он наполняется теми же
инструментами, что и обычная правка.
"""

from __future__ import annotations

import re
import zipfile

import pytest

from fscp_mcp import archive as arch
from fscp_mcp import drivers, edits, skeleton
from fscp_mcp.errors import FscpError

from . import factories

#: Что допустимо встретить текстом в обезличенном шаблоне: плейсхолдер, GUID,
#: число, булево, латинское слово-перечисление.
SAFE = re.compile(r"^(\{uid\d+\}|\{file_name\}|[0-9a-f-]{36}|-?\d+|true|false"
                  r"|[A-Za-z][A-Za-z0-9]*)$")

#: Названия, которые подставляет сама программа и которые от объекта не зависят.
STOCK = {"Корневая папка", "Контролируемый", "Опасный", "Критический"}


@pytest.fixture
def created(tmp_path):
    handle, parsed = None, None
    skeleton.create(tmp_path / "новый.fscp")
    handle, parsed = arch.open_archive(tmp_path / "новый.fscp")
    yield parsed
    arch.close(handle)


# ------------------------------------------------------------------ шаблон


@pytest.mark.parametrize("name", skeleton.TEMPLATES)
def test_шаблон_обезличен(name):
    """В репозиторий не должно просочиться ничего об объекте защиты.

    Скрипт tools/make_skeleton.py заменяет GUID плейсхолдерами и вычищает имя
    файла; этот тест — страховка на случай, если шаблон обновят вручную.
    """
    text = (skeleton.SKELETON / name).read_text(encoding="utf-8", newline="")

    подозрительное = sorted(
        {
            value.strip()
            for value in re.findall(r">([^<>]+)<", text)
            if value.strip()
            and not SAFE.match(value.strip())
            and value.strip() not in STOCK
            and value.strip().lower() not in drivers.table()
        }
    )
    assert подозрительное == [], подозрительное


@pytest.mark.parametrize("name", skeleton.TEMPLATES)
def test_шаблон_в_формате_xmlserializer(name):
    """Раскладка шаблона обязана совпадать с тем, что пишет Global Monitor."""
    from fscp_mcp import writer
    import xml.etree.ElementTree as ET

    raw = (skeleton.SKELETON / name).read_bytes()
    attrs, newline = writer.root_header(raw)
    assert newline == writer.CRLF, "шаблон пишется с CRLF"
    assert writer.serialize(ET.fromstring(raw), root_attrs=attrs, newline=newline) == raw


def test_каждый_созданный_файл_получает_свои_uid(tmp_path):
    """Тащить UID корня из чужой конфигурации незачем."""
    skeleton.create(tmp_path / "первый.fscp")
    skeleton.create(tmp_path / "второй.fscp")

    первый = zipfile.ZipFile(tmp_path / "первый.fscp").read("GKDeviceConfiguration.xml")
    второй = zipfile.ZipFile(tmp_path / "второй.fscp").read("GKDeviceConfiguration.xml")
    assert первый != второй

    guids = re.compile(rb"[0-9a-f-]{36}")
    assert set(guids.findall(первый)) != set(guids.findall(второй))


# ------------------------------------------------------------ созданный файл


def test_созданный_файл_открывается(created):
    assert created.version(created.gk) == "2.9"
    assert created.version(created.plans) == "2.6"
    assert created.root_device is not None
    assert created.root_device.driver.uid == drivers.LOCAL_NET
    assert created.objects_by_kind["zone"] == []
    assert created.plans_by_uid == {}


def test_имя_файла_попадает_внутрь_конфигурации(tmp_path):
    """Global Monitor пишет имя файла в SystemConfiguration — не подставив своё,
    мы оставили бы в новом файле чужое."""
    target = tmp_path / "объект №7.fscp"
    skeleton.create(target)

    system = zipfile.ZipFile(target).read("SystemConfiguration.xml").decode("utf-8")
    assert f"<FileName>{target.name}</FileName>" in system


def test_без_донора_записи_учёток_нет(tmp_path):
    """Хеши паролей не читаются и не выдумываются."""
    skeleton.create(tmp_path / "новый.fscp")
    names = zipfile.ZipFile(tmp_path / "новый.fscp").namelist()
    assert arch.SECURITY_CONFIG not in names


def test_донор_переносится_побайтово(tmp_path):
    donor = factories.build(tmp_path / "донор.fscp")
    target = tmp_path / "новый.fscp"

    result = skeleton.create(target, donor=donor)

    assert "донор.fscp" in str(result["security"])
    assert zipfile.ZipFile(target).read(arch.SECURITY_CONFIG) == zipfile.ZipFile(
        donor
    ).read(arch.SECURITY_CONFIG)


def test_донор_без_учёток_отвергается(tmp_path):
    donor = tmp_path / "пустой.fscp"
    with zipfile.ZipFile(donor, "w") as archive:
        archive.writestr("GKDeviceConfiguration.xml", b"<x />")

    with pytest.raises(FscpError, match="переносить нечего"):
        skeleton.create(tmp_path / "новый.fscp", donor=donor)


def test_созданный_файл_наполняется_обычными_инструментами(created, tmp_path):
    """Новый файл собирается теми же мутациями, что и правка существующего —
    отдельного кода под создание нет, и расходиться нечему."""
    сеть = created.root_device.children[0]
    (гк,) = edits.add_device(created, сеть.uid, "ГК")
    зона = edits.add_object(created, "zone", "Склад")
    сценарий = edits.add_object(created, "scenario", "ОПОВЕЩЕНИЕ")
    edits.add_clause(created, сценарий, targets=[зона], state="Пожар2")
    план = edits.add_plan(created, "Этаж 1")
    edits.place_object(created, план, гк, left=10, top=10)

    target = tmp_path / "наполненный.fscp"
    created.save(target)

    handle, reopened = arch.open_archive(target)
    try:
        assert гк in reopened.devices_by_uid
        assert len(reopened.objects_by_kind["zone"]) == 1
        assert len(reopened.plans_by_uid) == 1
        assert [uid for uid, _ in reopened.plan_objects_by_plan[план]] == [гк]
    finally:
        arch.close(handle)
