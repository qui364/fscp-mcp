from __future__ import annotations

import os

import pytest

from fscp_mcp import archive

from . import factories


def test_пустой_файл_даёт_понятную_ошибку(tmp_path):
    empty = factories.build_empty(tmp_path / "пустой.fscp")
    with pytest.raises(archive.FscpError) as excinfo:
        archive.open_archive(empty)
    assert "пустой" in str(excinfo.value)
    assert "Traceback" not in str(excinfo.value)


def test_несуществующий_файл(tmp_path):
    with pytest.raises(archive.FscpError, match="не найден"):
        archive.open_archive(tmp_path / "нет-такого.fscp")


def test_не_архив_даёт_понятную_ошибку(tmp_path):
    fake = factories.build_garbage(tmp_path / "мусор.fscp")
    with pytest.raises(archive.FscpError, match="не является ZIP"):
        archive.open_archive(fake)


def test_zip_без_конфигурации_устройств(tmp_path):
    """ZIP открывается, но это не конфигурация Рубеж."""
    chopped = factories.build_without_gk_config(tmp_path / "чужой.fscp")
    with pytest.raises(archive.FscpError, match="GKDeviceConfiguration.xml"):
        archive.open_archive(chopped)


def test_сводка(synthetic):
    summary = synthetic.summary()
    assert summary["versions"]["GKDeviceConfiguration.xml"] == factories.GK_VERSION
    assert summary["versions"]["PlansConfiguration.xml"] == factories.PLANS_VERSION
    assert summary["devices"] == factories.EXPECTED_DEVICES
    assert summary["gk_count"] == factories.EXPECTED_GK
    assert summary["objects"]["zone"] == factories.EXPECTED_ZONES


def test_шаблоны_параметров_не_считаются_устройствами(synthetic):
    """В ParameterTemplates лежат GKDevice-шаблоны — они не часть дерева."""
    template_devices = sum(
        1
        for template in synthetic.gk.iter("GKDeviceParameterTemplate")
        for _ in template.iter("GKDevice")
    )
    assert template_devices > 0
    assert all(
        device.element is not template_element
        for device in synthetic.devices_by_uid.values()
        for template in synthetic.gk.iter("GKDeviceParameterTemplate")
        for template_element in template.iter("GKDevice")
    )


def test_handle_переиспользуется_для_того_же_файла(synthetic_path):
    first, _ = archive.open_archive(synthetic_path)
    second, _ = archive.open_archive(synthetic_path)
    assert first == second


def test_изменение_файла_на_диске_обесценивает_handle(tmp_path):
    path = factories.build(tmp_path / "меняется.fscp")
    opened, _ = archive.open_archive(path)
    # mtime двигаем явно: запись сразу после создания файла может уложиться в
    # разрешение таймера файловой системы и оставить отметку прежней.
    stamp = path.stat().st_mtime + 10
    path.write_bytes(path.read_bytes() + b"\x00")
    os.utime(path, (stamp, stamp))
    with pytest.raises(archive.FscpError, match="изменился на диске"):
        archive.session(opened)


def test_неизвестный_handle():
    with pytest.raises(archive.FscpError, match="неизвестный handle"):
        archive.session("нет-такого")


def test_кэш_ограничен_по_размеру(tmp_path):
    for number in range(archive.MAX_SESSIONS + 2):
        archive.open_archive(factories.build(tmp_path / f"{number}.fscp"))
    assert len(archive.sessions()) <= archive.MAX_SESSIONS
