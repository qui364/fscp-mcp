"""Готовит обезличенный шаблон пустой конфигурации из рабочего файла .fscp.

Запускается руками, в пакет не входит и в обычной работе не нужен. Существует
потому, что пустой GKDeviceConfiguration - это 33 элемента верхнего уровня,
включая OPCSettings и настройки цветов охранных зон: выдумывать их нельзя, а
рабочую конфигурацию в репозиторий не выложить.

Скрипт вычищает всё объектное - дерево устройств обрезается до RootDevice и
«Локальной сети», коллекции опустошаются - и заменяет каждый оставшийся GUID
плейсхолдером. Остаются только настройки самой программы: цвета, OPC, звуки,
приоритеты инцидентов.

Результат надо просмотреть глазами перед коммитом. Скрипт для этого печатает
все текстовые значения, которые не являются GUID, числом, true/false или
ключевым словом перечисления, - если список не пуст, в шаблон просочились
данные объекта.

    py -3 tools/make_skeleton.py <исходник.fscp>

SecurityConfiguration.xml не читается и в шаблон не попадает: там хеши паролей.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import xml.etree.ElementTree as ET  # noqa: E402

from fscp_mcp import drivers, writer  # noqa: E402

TARGET = Path(__file__).resolve().parent.parent / "src" / "fscp_mcp" / "skeleton"

#: Коллекции, которые опустошаются целиком - в них живут объекты конкретного
#: объекта защиты.
EMPTIED = (
    "Zones",
    "GuardZones",
    "Directions",
    "PumpStations",
    "MPTs",
    "Delays",
    "Codes",
    "Cards",
    "Doors",
    "SKDZones",
    "ParameterTemplates",
    "CatalogueGKDoors",
    "CatalogueKDDoors",
    "GKCardTemplates",
    "CataloguePlans",
    "Plans",
    "Layouts",
    "JournalFilters",
    "Organisations",
    "CatalogueIncidents",
    "IncidentTypes",
    "IncidentFilters",
)

GUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                  r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
NIL = "00000000-0000-0000-0000-000000000000"

#: Что считается безобидным значением при проверке на данные объекта:
#: плейсхолдер, нулевой GUID, число, булево, одно латинское слово-перечисление.
SAFE = re.compile(
    r"^(|\{uid\d+\}|\{file_name\}|" + NIL + r"|-?\d+(\.\d+)?|true|false"
    r"|[A-Za-z][A-Za-z0-9]*)$"
)

#: Названия, которые Global Monitor подставляет сам и которые от объекта не
#: зависят: корневая папка каталога и штатные приоритеты инцидентов.
STOCK_NAMES = frozenset(
    {"Корневая папка", "Контролируемый", "Опасный", "Критический"}
)

ENTRIES = (
    "GKDeviceConfiguration.xml",
    "PlansConfiguration.xml",
    "LayoutsConfiguration.xml",
    "SystemConfiguration.xml",
)


def strip_devices(root: ET.Element) -> None:
    """Обрезает дерево до RootDevice + «Локальная сеть» без стволов."""
    device = root.find("RootDevice")
    if device is None:
        raise SystemExit("в конфигурации нет RootDevice")
    children = device.find("Children")
    if children is None or len(children) == 0:
        return
    local_net = children[0]
    for extra in list(children)[1:]:
        children.remove(extra)
    nested = local_net.find("Children")
    if nested is not None:
        for trunk in list(nested):
            nested.remove(trunk)
    for tag in ("Description", "PredefinedName", "ProjectAddress"):
        for owner in (device, local_net):
            found = owner.find(tag)
            if found is not None:
                owner.remove(found)


def empty_collections(root: ET.Element) -> None:
    for element in root.iter():
        if element.tag in EMPTIED:
            for child in list(element):
                element.remove(child)


def placeholderize(text: str) -> tuple[str, int]:
    """Заменяет GUID'ы плейсхолдерами - кроме нулевого и UID драйверов.

    UID драйверов - это общий справочник Рубеж, одинаковый во всех
    конфигурациях; он и так лежит в drivers.json. Подменив его, мы получили бы
    шаблон с несуществующим типом устройства.
    """
    catalogue = set(drivers.table())
    seen: dict[str, str] = {}

    def swap(match: re.Match[str]) -> str:
        value = match.group(0).lower()
        if value == NIL or value in catalogue:
            return match.group(0)
        if value not in seen:
            seen[value] = f"{{uid{len(seen)}}}"
        return seen[value]

    return GUID.sub(swap, text), len(seen)


def strip_file_name(text: str) -> str:
    """Убирает имя исходного файла: Global Monitor пишет его внутрь конфигурации.

    Оставить его в шаблоне значило бы выложить в репозиторий кусок имени
    рабочего объекта, а в созданном файле - чужое имя вместо своего.
    """
    return re.sub(
        r"<FileName>[^<]*</FileName>", "<FileName>{file_name}</FileName>", text
    )


def suspicious(text: str) -> list[str]:
    """Текстовые значения, не похожие на настройку программы."""
    found = []
    for value in re.findall(r">([^<>]+)<", text):
        clean = value.strip()
        if clean.lower() in drivers.table():
            continue
        if clean and not SAFE.match(clean) and clean not in STOCK_NAMES:
            found.append(clean)
    return sorted(set(found))


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    source = Path(sys.argv[1])
    TARGET.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(source) as archive:
        available = archive.namelist()
        for name in ENTRIES:
            if name not in available:
                print(f"  {name}: в исходнике нет, пропущено")
                continue
            raw = archive.read(name)
            attrs, _ = writer.root_header(raw)
            root = ET.fromstring(raw)

            if name == "GKDeviceConfiguration.xml":
                strip_devices(root)
            empty_collections(root)

            # Шаблон всегда с CRLF: так пишет Global Monitor в подавляющем
            # большинстве записей, и новый файл должен выглядеть как его.
            text = writer.serialize(
                root, root_attrs=attrs, newline=writer.CRLF
            ).decode("utf-8")
            text, count = placeholderize(text)
            text = strip_file_name(text)

            out = TARGET / name
            out.write_text(text, encoding="utf-8", newline="")
            leftovers = suspicious(text)
            print(f"  {name}: {len(text)} байт, GUID заменено {count}")
            if leftovers:
                print(f"    ПРОВЕРЬТЕ ГЛАЗАМИ, похоже на данные объекта: {leftovers}")

    print(f"\nШаблон записан в {TARGET}")
    print("Просмотрите файлы перед коммитом: в них не должно быть ничего об объекте.")


if __name__ == "__main__":
    main()
