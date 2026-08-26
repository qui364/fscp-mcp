"""Сериализатор XML и пересборка архива.

Главное утверждение всего модуля записи: разобранный и записанный обратно файл
совпадает с исходным **побайтово**. Тогда любое расхождение в файле, открытом
в Global Monitor, - это наша правка, а не артефакт сериализации. Здесь это
проверяется на синтетике, в test_real_configs.py - на рабочих конфигурациях.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile

import pytest

from fscp_mcp import archive as arch
from fscp_mcp import writer
from fscp_mcp.errors import FscpError

from . import factories

XML_ENTRIES = (
    "GKDeviceConfiguration.xml",
    "PlansConfiguration.xml",
    "LayoutsConfiguration.xml",
    "SystemConfiguration.xml",
)


def _roundtrip(raw: bytes) -> bytes:
    attrs, newline = writer.root_header(raw)
    return writer.serialize(ET.fromstring(raw), root_attrs=attrs, newline=newline)


@pytest.mark.parametrize("newline", ["\r\n", "\n"], ids=["crlf", "lf"])
@pytest.mark.parametrize("entry", XML_ENTRIES)
def test_круг_разбор_сериализация_замкнут(tmp_path, newline, entry):
    """Разбор и сериализация не теряют и не добавляют ни байта.

    Синтетика собрана тем же сериализатором, поэтому проверка замкнутая - что
    раскладка совпадает с настоящим Global Monitor, утверждает
    test_real_configs.py на рабочих конфигурациях. Здесь ловится другое:
    потеря xmlns, xsi:nil или перевода строки при проходе через ZIP.
    """
    path = factories.build(tmp_path / "синтетика.fscp", newline=newline)
    raw = zipfile.ZipFile(path).read(entry)
    assert _roundtrip(raw) == raw


def test_перевод_строки_берётся_из_записи_а_не_из_настройки(tmp_path):
    """В одном архиве записи бывают с разным переводом строки."""
    crlf = zipfile.ZipFile(
        factories.build(tmp_path / "crlf.fscp", newline="\r\n")
    ).read("GKDeviceConfiguration.xml")
    lf = zipfile.ZipFile(
        factories.build(tmp_path / "lf.fscp", newline="\n")
    ).read("GKDeviceConfiguration.xml")

    assert b"\r\n" in crlf and b"\r" not in lf
    assert writer.root_header(crlf)[1] == "\r\n"
    assert writer.root_header(lf)[1] == "\n"


def test_объявления_пространств_имён_сохраняются_дословно(tmp_path):
    """ElementTree теряет xmlns, если префикс нигде не использован.

    У GKDeviceConfiguration объявлены оба префикса, а xsd не используется -
    наивная сериализация выбросила бы его и файл перестал бы совпадать.
    """
    path = factories.build(tmp_path / "синтетика.fscp")
    raw = zipfile.ZipFile(path).read("GKDeviceConfiguration.xml")

    attrs, _ = writer.root_header(raw)
    assert 'xmlns:xsd="http://www.w3.org/2001/XMLSchema"' in attrs
    assert "XMLSchema-instance" in attrs
    assert b"XMLSchema-instance" in _roundtrip(raw)


def test_атрибут_xsi_nil_переживает_круг(tmp_path):
    path = factories.build(tmp_path / "синтетика.fscp")
    raw = zipfile.ZipFile(path).read("PlansConfiguration.xml")

    assert b'xsi:nil="true"' in raw
    assert _roundtrip(raw).count(b'xsi:nil="true"') == raw.count(b'xsi:nil="true"')


def test_пустой_элемент_самозакрывается_с_пробелом():
    """<Foo /> - именно так пишет XmlSerializer, и на это завязан round-trip."""
    root = ET.fromstring("<Root><Empty /></Root>")
    assert b"<Empty />" in writer.serialize(root)


def test_спецсимволы_экранируются():
    """В рабочих конфигурациях сущностей не встретилось, но описание вводит
    пользователь - экранирование может проверить только синтетика."""
    root = ET.fromstring("<Root><D>А &amp; Б &lt;тест&gt;</D></Root>")
    out = writer.serialize(root).decode("utf-8")

    assert "&amp;" in out and "&lt;" in out and "&gt;" in out
    assert ET.fromstring(out).findtext("D") == "А & Б <тест>"


def test_смешанное_содержимое_отвергается():
    """Формат такого не использует; молча потерять текст хуже, чем упасть."""
    root = ET.fromstring("<Root>текст<Child /></Root>")
    with pytest.raises(FscpError, match="[Сс]мешанное"):
        writer.serialize(root)


# ------------------------------------------------------------ пересборка ZIP


def test_сохранение_без_правок_не_меняет_ни_одной_записи(tmp_path):
    source = factories.build(tmp_path / "исходник.fscp")
    _, archive = arch.open_archive(source)
    target = tmp_path / "копия.fscp"

    archive.save(target)

    before, after = zipfile.ZipFile(source), zipfile.ZipFile(target)
    assert before.namelist() == after.namelist()
    for name in before.namelist():
        assert before.read(name) == after.read(name), name


def test_метаданные_записей_сохраняются(tmp_path):
    """У блобов Content/ собственные таймстампы, у директорий - stored."""
    source = factories.build(tmp_path / "исходник.fscp")
    _, archive = arch.open_archive(source)
    target = tmp_path / "копия.fscp"

    archive.save(target)

    before, after = zipfile.ZipFile(source), zipfile.ZipFile(target)
    for name in before.namelist():
        old, new = before.getinfo(name), after.getinfo(name)
        assert old.date_time == new.date_time, name
        assert old.compress_type == new.compress_type, name
        assert old.external_attr == new.external_attr, name

    blob = after.getinfo(f"Content/{factories.PNG_GUID}")
    assert blob.date_time == factories.BLOB_DATE_TIME
    assert blob.date_time != after.getinfo("GKDeviceConfiguration.xml").date_time


def test_сохранение_в_исходник_отвергается(tmp_path):
    source = factories.build(tmp_path / "исходник.fscp")
    _, archive = arch.open_archive(source)

    with pytest.raises(FscpError, match="перезапись исходного файла"):
        archive.save(source)


def test_существующий_файл_не_затирается_без_разрешения(tmp_path):
    source = factories.build(tmp_path / "исходник.fscp")
    _, archive = arch.open_archive(source)
    target = tmp_path / "занято.fscp"
    guard = "не трогать".encode("utf-8")
    target.write_bytes(guard)

    with pytest.raises(FscpError, match="уже существует"):
        archive.save(target)
    assert target.read_bytes() == guard

    archive.save(target, overwrite=True)
    assert target.read_bytes() != guard


def test_сохранённый_файл_открывается_обратно(tmp_path):
    source = factories.build(tmp_path / "исходник.fscp")
    _, archive = arch.open_archive(source)
    target = tmp_path / "копия.fscp"
    archive.save(target)

    _, reopened = arch.open_archive(target)
    assert len(reopened.devices_by_uid) == factories.EXPECTED_DEVICES
    assert reopened.devices_by_address[factories.IP_ADDRESS].name == factories.IP_NAME


def test_security_config_переносится_но_не_разбирается(tmp_path, monkeypatch):
    """«Не читать» значит «не разбирать и не показывать», а не «потерять».

    Без этой записи архив неполон, поэтому байты переносятся - но если кто-то
    когда-нибудь заведёт её разбор, тест упадёт.
    """
    source = factories.build(tmp_path / "исходник.fscp")
    _, archive = arch.open_archive(source)

    real = ET.fromstring

    def guard(data, *args, **kwargs):
        assert b"SecurityConfiguration" not in data[:200], "разбор запрещён"
        return real(data, *args, **kwargs)

    monkeypatch.setattr(ET, "fromstring", guard)
    target = tmp_path / "копия.fscp"
    archive.save(target)

    assert zipfile.ZipFile(target).read("SecurityConfiguration.xml") == zipfile.ZipFile(
        source
    ).read("SecurityConfiguration.xml")
