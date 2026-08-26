"""Открытие архива .fscp и построение индексов.

.fscp - это ZIP с XML-конфигурациями (выхлоп .NET XmlSerializer) и подложками
планов в Content/ (PNG либо JPEG, без расширения). Рабочая конфигурация доходит
до 25 МБ XML и 414 тыс. элементов,
поэтому разбор делается один раз на открытие, а результат живёт в сессионном кэше:
иначе каждый вызов инструмента стоил бы ~1.3 с и ~150 МБ.

В охвате только GKDeviceConfiguration.xml, PlansConfiguration.xml и Content/*.
SecurityConfiguration.xml намеренно не читается - там SHA-512 хеши паролей
пользователей.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any

from . import drivers, images, writer
from .errors import FscpError

GK_CONFIG = "GKDeviceConfiguration.xml"
PLANS_CONFIG = "PlansConfiguration.xml"
SECURITY_CONFIG = "SecurityConfiguration.xml"
CONTENT_PREFIX = "Content/"

#: Записи, которые сервер только перечисляет, но не разбирает.
UNPARSED_CONFIGS = ("SystemConfiguration.xml", "LayoutsConfiguration.xml")

#: Контейнеры верхнего уровня GKDeviceConfiguration и вид объектов в них.
COLLECTIONS: dict[str, str] = {
    "Zones": "zone",
    "GuardZones": "guard_zone",
    "Directions": "direction",
    "Delays": "scenario",
    "MPTs": "mpt",
    "PumpStations": "pump_station",
    "Doors": "door",
    "SKDZones": "skd_zone",
    "ParameterTemplates": "parameter_template",
}

#: Поля планов, в которых может стоять ссылка на картинку.
IMAGE_REF_FIELDS = ("BackgroundImageSource", "BackgroundSVGImageSource", "ImageSource")

NIL_UID = "00000000-0000-0000-0000-000000000000"

#: Контейнеры, чьи дети <guid> - ссылки на другие объекты.
UID_LIST_TAGS = frozenset(
    {
        "ZoneUIDs",
        "GuardZoneUIDs",
        "DeviceUIDs",
        "DirectionUIDs",
        "DelayUIDs",
        "DoorUIDs",
        "MPTUIDs",
        "PumpStationsUIDs",
        "NSUIDs",
        "PlanElementUIDs",
        "MirrorUsers",
    }
)

#: Скалярные поля-ссылки. UID - это сам объект, а DriverUID - справочник, и
#: ни то ни другое ссылкой не является.
SCALAR_UID_TAGS = frozenset(
    {
        "ItemUID",
        "ReserveGkUID",
        "EnterZoneUID",
        "ExitZoneUID",
        "PumpStationUID",
        "DoorUID",
        "PimUID",
        "PlanUID",
        "DeviceUid",
        "IncidentUID",
        "LocationUID",
    }
)


@dataclass(frozen=True, slots=True)
class Reference:
    """Одна ссылка на объект по GUID - и как её вычистить.

    holder держит элемент, из которого убирать: для <guid> в списке это сам
    список, для скалярного поля - родитель. element - то, что правится.
    """

    entry: str
    tag: str
    element: ET.Element
    holder: ET.Element
    in_list: bool


_SESSIONS: dict[str, FscpArchive] = {}
_HANDLES = count(1)
MAX_SESSIONS = 3


#: FscpError объявлен в errors.py - он нужен и writer.py, который по слоям
#: ниже архива. Имя оставлено здесь: на него завязаны server.py, views.py и тесты.
__all__ = ["FscpError", "FscpArchive", "Device", "ObjectRef", "open_archive"]


@dataclass(slots=True)
class Device:
    """Узел дерева устройств с вычисленным адресом."""

    uid: str
    element: ET.Element
    driver_uid: str
    int_address: int
    parent: Device | None = None
    address: str = ""
    children: list[Device] = field(default_factory=list)

    @property
    def driver(self) -> drivers.Driver:
        return drivers.get(self.driver_uid)

    @property
    def predefined_name(self) -> str:
        return (self.element.findtext("PredefinedName") or "").strip()

    @property
    def description(self) -> str:
        return (self.element.findtext("Description") or "").strip()

    def property_value(self, name: str) -> str:
        """Значение GKProperty: числовое в Value, строковое в StringValue."""
        container = self.element.find("Properties")
        if container is None:
            return ""
        for prop in container.findall("GKProperty"):
            if (prop.findtext("Name") or "").strip() == name:
                string_value = (prop.findtext("StringValue") or "").strip()
                return string_value or (prop.findtext("Value") or "").strip()
        return ""

    @property
    def name(self) -> str:
        """Отображаемое имя: у устройств в конфигурации нет поля Name.

        Правило выведено сверкой с полем Name объектов на планах, где то же имя
        уже посчитано самим Global Monitor.
        """
        short = self.driver.short
        if self.driver_uid == drivers.GK:
            # ГК показывается по своему IP, а не по номеру.
            ip = self.property_value("IPAddress")
            return f"{short} {ip}".strip()
        name = f"{short} {self.address}".strip()
        if self.description:
            name += f"({self.description})"
        return name

    def brief(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "name": self.name,
            "address": self.address,
            "driver": self.driver.short,
            "children": len(self.children),
        }


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """Объект верхнего уровня GKDeviceConfiguration (зона, сценарий, ...)."""

    kind: str
    uid: str
    element: ET.Element

    @property
    def no(self) -> str:
        return (self.element.findtext("No") or "").strip()

    @property
    def name(self) -> str:
        raw = (self.element.findtext("Name") or "").strip()
        return f"{self.no}.{raw}" if self.no and raw else raw or f"<{self.kind}>"

    def brief(self) -> dict[str, Any]:
        return {"uid": self.uid, "kind": self.kind, "no": self.no, "name": self.name}


def text(element: ET.Element, tag: str) -> str:
    return (element.findtext(tag) or "").strip()


def uid_of(element: ET.Element, tag: str = "UID") -> str:
    return text(element, tag).lower()


def guid_list(element: ET.Element | None) -> list[str]:
    if element is None:
        return []
    return [(g.text or "").strip().lower() for g in element.findall("guid") if g.text]


class FscpArchive:
    """Разобранный архив .fscp вместе с индексами."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.mtime = path.stat().st_mtime
        self.entries: list[dict[str, Any]] = []

        #: Полные ZipInfo исходника: при сохранении неизменённые записи
        #: копируются побайтово с их же compress_type, date_time и порядком.
        self.source_entries: list[zipfile.ZipInfo] = []
        #: На каждую XML-запись - дословная xmlns-строка корня и перевод строки.
        #: Из дерева не восстанавливаются, а без них round-trip не побайтовый.
        self.headers: dict[str, tuple[str, str]] = {}
        #: Записи архива, которые правились в памяти и требуют пересборки.
        self.dirty: set[str] = set()
        #: Журнал правок для fscp_diff. Заполняет edits.py.
        self.journal: list[Any] = []
        #: Новые записи архива, которых в исходнике не было, - подложки
        #: планов, добавленные правкой. Дописываются в конец при сохранении.
        self.added_entries: dict[str, bytes] = {}
        #: Обратный индекс «на кого ссылаются»: без него удаление объекта
        #: оставило бы висячие GUID'ы в ZoneUIDs, логике и на планах.
        self.refs_to: dict[str, list[Reference]] = {}
        #: Сжатые исходные байты разбираемых записей - снимок для fscp_revert.
        #: Дерево ET стоит вшестеро дороже своего XML, так что сырые байты -
        #: самый дешёвый снимок, а не самый дорогой: 25 МБ ужимаются до ~1 МБ.
        self.baseline: dict[str, bytes] = {}

        self.gk: ET.Element
        self.plans: ET.Element | None = None

        self.root_device: Device | None = None
        self.devices_by_uid: dict[str, Device] = {}
        self.devices_by_address: dict[str, Device] = {}
        self.gk_devices: list[Device] = []

        self.objects_by_uid: dict[str, ObjectRef] = {}
        self.objects_by_kind: dict[str, list[ObjectRef]] = {}

        self.plans_by_uid: dict[str, ET.Element] = {}
        self.plan_parent: dict[str, str | None] = {}
        self.plan_children: dict[str, list[str]] = {}
        self.plan_roots: list[str] = []
        self.plan_objects_by_item: dict[str, list[tuple[str, ET.Element]]] = {}
        self.plan_objects_by_plan: dict[str, list[tuple[str, ET.Element]]] = {}

        #: Обратная привязка «зона -> устройства»: прямой поиск требовал бы
        #: обхода всех 5656 устройств на каждый вызов.
        self.devices_by_zone: dict[str, list[Device]] = {}

        self.images: dict[str, images.ImageInfo] = {}
        self.image_refs: dict[str, list[str]] = {}
        self.resource_refs: set[str] = set()
        self.missing_image_refs: dict[str, list[str]] = {}

        self._load()

    # -------------------------------------------------------------- загрузка

    def _load(self) -> None:
        try:
            with zipfile.ZipFile(self.path) as archive:
                self._read_entries(archive)
                gk_bytes = self._require(archive, GK_CONFIG)
                self.gk = ET.fromstring(gk_bytes)
                self.headers[GK_CONFIG] = writer.root_header(gk_bytes)
                self.baseline[GK_CONFIG] = zlib.compress(gk_bytes, 1)
                if PLANS_CONFIG in archive.namelist():
                    plans_bytes = archive.read(PLANS_CONFIG)
                    self.plans = ET.fromstring(plans_bytes)
                    self.headers[PLANS_CONFIG] = writer.root_header(plans_bytes)
                    self.baseline[PLANS_CONFIG] = zlib.compress(plans_bytes, 1)
                self._index_images(archive)
        except zipfile.BadZipFile as exc:
            raise FscpError(
                f"{self.path.name}: файл не является ZIP-архивом .fscp "
                f"(размер {self.path.stat().st_size} байт)"
            ) from exc
        except ET.ParseError as exc:
            raise FscpError(
                f"{self.path.name}: не удалось разобрать XML - {exc}"
            ) from exc

        self._reindex()

    def _reindex(self) -> None:
        """Пересобирает все индексы от живого дерева.

        Зовётся после каждой мутации: на самой большой конфигурации (5192
        устройства) это 0,16 с - дешевле и надёжнее инкрементальной
        инвалидации. Заодно чинится ловушка с id(node) в _index_plans:
        множество узлов вложенных планов строится заново.
        """
        self.root_device = None
        self.devices_by_uid = {}
        self.devices_by_address = {}
        self.gk_devices = []
        self.devices_by_zone = {}
        self.objects_by_uid = {}
        self.objects_by_kind = {}
        self.plans_by_uid = {}
        self.plan_parent = {}
        self.plan_children = {}
        self.plan_roots = []
        self.plan_objects_by_item = {}
        self.plan_objects_by_plan = {}
        self.refs_to = {}

        self._index_devices()
        self._index_objects()
        self._index_plans()
        self._index_refs()

    def _read_entries(self, archive: zipfile.ZipFile) -> None:
        for info in archive.infolist():
            self.source_entries.append(info)
            self.entries.append(
                {
                    "name": info.filename,
                    "size_bytes": info.file_size,
                    "compressed_bytes": info.compress_size,
                    "modified": "%04d-%02d-%02d %02d:%02d:%02d" % info.date_time,
                    "is_dir": info.is_dir(),
                }
            )

    def _require(self, archive: zipfile.ZipFile, name: str) -> bytes:
        if name not in archive.namelist():
            raise FscpError(
                f"{self.path.name}: в архиве нет {name} - это не конфигурация Рубеж"
            )
        return archive.read(name)

    # --------------------------------------------------------------- индексы

    def _index_devices(self) -> None:
        root_element = self.gk.find("RootDevice")
        if root_element is None:
            raise FscpError(f"{self.path.name}: в {GK_CONFIG} нет RootDevice")

        trunk_number = count(1)

        def build(element: ET.Element, parent: Device | None, depth: int) -> Device:
            try:
                int_address = int(text(element, "IntAddress") or 0)
            except ValueError:
                int_address = 0

            device = Device(
                uid=uid_of(element),
                element=element,
                driver_uid=text(element, "DriverUID").lower(),
                int_address=int_address,
                parent=parent,
            )
            device.address = self._address_for(device, parent, depth, trunk_number)

            if device.driver_uid == drivers.GK:
                self.gk_devices.append(device)
            if device.uid:
                self.devices_by_uid[device.uid] = device
            if device.address:
                self.devices_by_address.setdefault(device.address, device)

            for tag in ("ZoneUIDs", "GuardZoneUIDs"):
                for zone_uid in guid_list(element.find(tag)):
                    self.devices_by_zone.setdefault(zone_uid, []).append(device)

            children = element.find("Children")
            if children is not None:
                for child in children.findall("GKDevice"):
                    device.children.append(build(child, device, depth + 1))
            return device

        self.root_device = build(root_element, None, 0)

    @staticmethod
    def _address_for(
        device: Device, parent: Device | None, depth: int, trunk_number: count
    ) -> str:
        """Вычисляет адрес устройства вида 1.2.1.1.

        Правило выведено сверкой с полем Name объектов на планах — там тот же
        адрес уже посчитан самим Global Monitor:

        * дети «Локальной сети» (РСГК или ГК) нумеруются по порядку - это первый
          компонент адреса;
        * объект с флагом no_address (ГК, «Группа реле», «Линия БМП», ...) сам
          адреса не имеет и показывает адрес родителя;
        * дети сквозного узла - группового устройства (МВК4, РМ2, АМ4, ...) или
          того же no_address-контейнера - стоят на одном уровне с ним, а не
          уровнем ниже: они несут абсолютный IntAddress, который заменяет
          последний компонент адреса родителя;
        * в остальных случаях IntAddress дописывается к адресу родителя.
        """
        if depth == 0:
            return ""
        if depth == 1:
            return str(next(trunk_number))
        if parent is None:
            return ""
        if device.driver.no_address:
            return parent.address
        if (parent.driver.is_group or parent.driver.no_address) and parent.address:
            head = parent.address.rpartition(".")[0]
            return f"{head}.{device.int_address}" if head else str(device.int_address)
        if parent.address:
            return f"{parent.address}.{device.int_address}"
        return ""

    def _index_objects(self) -> None:
        for container, kind in COLLECTIONS.items():
            found = self.gk.find(container)
            bucket: list[ObjectRef] = []
            if found is not None:
                for element in list(found):
                    ref = ObjectRef(kind=kind, uid=uid_of(element), element=element)
                    if ref.uid:
                        self.objects_by_uid[ref.uid] = ref
                    bucket.append(ref)
            self.objects_by_kind[kind] = bucket

    def _index_plans(self) -> None:
        if self.plans is None:
            return
        container = self.plans.find("Plans")
        if container is None:
            return

        def walk(element: ET.Element, parent_uid: str | None) -> None:
            plan_uid = uid_of(element)
            self.plans_by_uid[plan_uid] = element
            self.plan_parent[plan_uid] = parent_uid
            self.plan_children.setdefault(plan_uid, [])
            if parent_uid is None:
                self.plan_roots.append(plan_uid)
            else:
                self.plan_children.setdefault(parent_uid, []).append(plan_uid)

            children = element.find("Children")
            # Объекты вложенных планов принадлежат им, а не родителю, поэтому
            # поддерево Children из обхода исключается. Множество id строится
            # один раз на план: проверка вхождения обходом дала бы квадрат.
            nested = {id(node) for node in children.iter()} if children is not None else set()

            for node in element.iter():
                if id(node) in nested:
                    continue
                item_uid = text(node, "ItemUID").lower()
                if item_uid and item_uid != NIL_UID:
                    self.plan_objects_by_item.setdefault(item_uid, []).append(
                        (plan_uid, node)
                    )
                    self.plan_objects_by_plan.setdefault(plan_uid, []).append(
                        (item_uid, node)
                    )
                for field_name in IMAGE_REF_FIELDS:
                    self._note_image_ref(text(node, field_name), plan_uid)

            if children is not None:
                for child in children.findall("Plan"):
                    walk(child, plan_uid)

        for plan in container.findall("Plan"):
            walk(plan, None)

        for guid, plan_uids in self.image_refs.items():
            if guid not in self.images:
                self.missing_image_refs[guid] = plan_uids

    def _index_refs(self) -> None:
        """Собирает обратный индекс ссылок по обоим деревьям.

        Отдельным проходом, а не внутри индексации объектов: ссылки живут в
        произвольной глубине (логика вложена в устройство, объекты - в план),
        и общий обход короче и понятнее, чем врезки в три разных индексатора.
        На 25-МБ конфигурации это 0,06 с при 11 894 ссылках.
        """
        for entry, root in ((GK_CONFIG, self.gk), (PLANS_CONFIG, self.plans)):
            if root is None:
                continue
            for parent in root.iter():
                if parent.tag in UID_LIST_TAGS:
                    for item in parent.findall("guid"):
                        self._note_ref(entry, parent.tag, item, parent, True)
                    continue
                for child in parent:
                    if child.tag in SCALAR_UID_TAGS and len(child) == 0:
                        self._note_ref(entry, child.tag, child, parent, False)

    def _note_ref(
        self,
        entry: str,
        tag: str,
        element: ET.Element,
        holder: ET.Element,
        in_list: bool,
    ) -> None:
        uid = (element.text or "").strip().lower()
        if not uid or uid == NIL_UID:
            return
        self.refs_to.setdefault(uid, []).append(
            Reference(entry=entry, tag=tag, element=element, holder=holder, in_list=in_list)
        )

    def referrers(self, uid: str) -> list[Reference]:
        """Кто ссылается на объект. Пусто - удалять безопасно."""
        return self.refs_to.get(uid.strip().lower(), [])

    def _note_image_ref(self, value: str, plan_uid: str) -> None:
        """Ссылка на картинку бывает двух видов: guid записи Content/ и путь
        ресурса самого Global Monitor (GKModule/Images/Zone.png) - последнего
        в архиве нет и быть не должно."""
        if not value:
            return
        if "/" in value or value.lower().endswith(".png"):
            self.resource_refs.add(value)
            return
        guid = value.lower()
        refs = self.image_refs.setdefault(guid, [])
        if plan_uid not in refs:
            refs.append(plan_uid)

    def _index_images(self, archive: zipfile.ZipFile) -> None:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.startswith(CONTENT_PREFIX):
                continue
            guid = info.filename[len(CONTENT_PREFIX) :].lower()
            with archive.open(info) as stream:
                head = stream.read(images.HEAD_BYTES)
            self.images[guid] = images.describe(
                guid=guid, entry=info.filename, size_bytes=info.file_size, head=head
            )

    # ---------------------------------------------------------------- доступ

    def blob(self, guid: str) -> bytes:
        info = self.images.get(guid.lower())
        if info is None:
            raise FscpError(f"в архиве нет подложки {guid}")
        added = self.added_entries.get(info.entry)
        if added is not None:
            return added
        with zipfile.ZipFile(self.path) as archive:
            return archive.read(info.entry)

    def raw(self, entry: str) -> bytes:
        with zipfile.ZipFile(self.path) as archive:
            if entry not in archive.namelist():
                raise FscpError(f"в архиве нет записи {entry}")
            return archive.read(entry)

    def device(self, uid_or_address: str) -> Device:
        key = uid_or_address.strip().lower()
        found = self.devices_by_uid.get(key) or self.devices_by_address.get(key)
        if found is None:
            raise FscpError(
                f"не найдено устройство по '{uid_or_address}' "
                f"(ожидается UID или адрес вида 1.2.1.1)"
            )
        return found

    def object(self, uid: str, kind: str | None = None) -> ObjectRef:
        ref = self.objects_by_uid.get(uid.strip().lower())
        if ref is None:
            raise FscpError(f"не найден объект {uid}")
        if kind and ref.kind != kind:
            raise FscpError(f"объект {uid} - это {ref.kind}, а не {kind}")
        return ref

    @staticmethod
    def version(element: ET.Element | None) -> str:
        if element is None:
            return ""
        version = element.find("Version")
        if version is None:
            return ""
        return f"{text(version, 'MajorVersion')}.{text(version, 'MinorVersion')}"

    def summary(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "size_bytes": self.path.stat().st_size,
            "versions": {
                GK_CONFIG: self.version(self.gk),
                PLANS_CONFIG: self.version(self.plans),
            },
            "devices": len(self.devices_by_uid),
            "gk_count": len(self.gk_devices),
            "objects": {k: len(v) for k, v in self.objects_by_kind.items() if v},
            "plans": len(self.plans_by_uid),
            "plan_roots": len(self.plan_roots),
            "images": len(self.images),
        }

    # ------------------------------------------------------------ сохранение

    def entry_bytes(self, name: str) -> bytes:
        """Байты записи для сохранения: изменённая - из дерева, прочие - из файла."""
        if name in self.dirty:
            root = self.gk if name == GK_CONFIG else self.plans
            if root is None:
                raise FscpError(f"{name}: запись помечена изменённой, но дерева нет")
            attrs, newline = self.headers.get(name, ("", writer.CRLF))
            return writer.serialize(root, root_attrs=attrs, newline=newline)
        return self.raw(name)

    def revert(self) -> dict[str, Any]:
        """Возвращает деревья к состоянию на момент открытия.

        Разбираются заново сохранённые при открытии байты, а не файл с диска:
        исходник мог с тех пор смениться, а откат должен вести именно туда,
        откуда сессия стартовала.
        """
        undone = len(self.journal)
        entries = sorted(self.dirty)
        for name in entries:
            raw = self.baseline.get(name)
            if raw is None:
                raise FscpError(f"{name}: нет снимка для отката")
            root = ET.fromstring(zlib.decompress(raw))
            if name == GK_CONFIG:
                self.gk = root
            else:
                self.plans = root
        self.dirty.clear()
        self.journal.clear()
        if entries:
            self._reindex()
        return {"reverted": undone, "entries": entries}

    def save(self, target: Path, *, overwrite: bool = False) -> dict[str, Any]:
        """Собирает архив заново по указанному пути. Исходник не трогается.

        Неизменённые записи копируются побайтово вместе с их метаданными -
        включая собственные таймстампы блобов Content/*, - и в исходном
        порядке: он между версиями Global Monitor не стабилен, и навязывать
        свой значит менять файл там, где мы ничего не правили.

        SecurityConfiguration.xml переносится как непрозрачные байты: он не
        разбирается и наружу не отдаётся, но без него архив неполон.
        """
        if target == self.path:
            raise FscpError(
                f"{target.name}: перезапись исходного файла не поддерживается, "
                "укажите другой путь"
            )
        if target.exists() and not overwrite:
            raise FscpError(
                f"{target} уже существует; передайте overwrite=true, чтобы заменить"
            )
        if not self.path.exists():
            raise FscpError(f"исходник {self.path} исчез; сохранить нечем")
        if self.path.stat().st_mtime != self.mtime:
            raise FscpError(
                f"{self.path.name} изменился на диске; неизменённые записи берутся "
                "из него, поэтому сохранение отменено - откройте архив заново"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().timetuple()[:6]
        # Новые записи получают тот же external_attr, что и остальные в этом
        # архиве: он разный у разных версий Global Monitor, и своё значение
        # выглядело бы в файле чужеродно.
        default_attr = (
            self.source_entries[0].external_attr if self.source_entries else 0
        )
        temporary = target.with_name(target.name + ".tmp")

        try:
            with zipfile.ZipFile(temporary, "w") as out:
                for info in self.source_entries:
                    fresh = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                    fresh.compress_type = info.compress_type
                    fresh.external_attr = info.external_attr
                    fresh.create_system = info.create_system
                    if info.filename in self.dirty and not info.is_dir():
                        fresh.date_time = stamp
                    payload = b"" if info.is_dir() else self.entry_bytes(info.filename)
                    out.writestr(fresh, payload)
                    # zipfile при записи подменяет нулевой external_attr на
                    # 0o600 << 16, а в рабочих конфигурациях он как раз нулевой.
                    # Центральный каталог пишется при close(), так что вернуть
                    # исходное значение объекту достаточно.
                    fresh.external_attr = info.external_attr

                for name, payload in self.added_entries.items():
                    fresh = zipfile.ZipInfo(name, date_time=stamp)
                    fresh.compress_type = zipfile.ZIP_DEFLATED
                    out.writestr(fresh, payload)
                    fresh.external_attr = default_attr
            temporary.replace(target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise FscpError(f"не удалось записать {target}: {exc}") from exc

        return {
            "path": str(target),
            "size_bytes": target.stat().st_size,
            "entries": len(self.source_entries) + len(self.added_entries),
            "rewritten": sorted(self.dirty),
        }


# ----------------------------------------------------------------- сессии


def open_archive(path: str | Path) -> tuple[str, FscpArchive]:
    """Открывает архив и кладёт его в сессионный кэш, возвращая handle."""
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = (Path.cwd() / resolved).resolve()
    if not resolved.exists():
        raise FscpError(f"файл не найден: {resolved}")
    if not resolved.is_file():
        raise FscpError(f"это не файл: {resolved}")
    if resolved.stat().st_size == 0:
        raise FscpError(f"{resolved.name}: файл пустой (0 байт)")

    mtime = resolved.stat().st_mtime
    for handle, cached in _SESSIONS.items():
        # Тот же файл в том же состоянии - отдаём ту же сессию вместе с
        # накопленными в ней правками: разбирать 25 МБ второй раз незачем,
        # а две независимые сессии на один файл затирали бы друг друга.
        if cached.path == resolved and cached.mtime == mtime:
            return handle, cached

    _evict()
    archive = FscpArchive(resolved)
    handle = f"fscp{next(_HANDLES)}"
    _SESSIONS[handle] = archive
    return handle, archive


def _evict() -> None:
    """Освобождает место под новый архив, не трогая несохранённые правки.

    Вытеснение идёт по порядку открытия, но грязные сессии пропускаются: там
    лежат правки, которых больше нигде нет. Если чистых не осталось - отказ с
    перечислением handle'ов, а не молчаливая потеря работы.
    """
    while len(_SESSIONS) >= MAX_SESSIONS:
        victim = next((h for h, a in _SESSIONS.items() if not a.dirty), None)
        if victim is None:
            busy = ", ".join(
                f"{h} ({len(a.journal)} правок)" for h, a in _SESSIONS.items()
            )
            raise FscpError(
                f"открыто {len(_SESSIONS)} архивов, и во всех есть несохранённые "
                f"правки: {busy}. Сохраните через fscp_save или откатите через "
                "fscp_revert, либо закройте лишний через fscp_close"
            )
        _SESSIONS.pop(victim)


def session(handle: str) -> FscpArchive:
    archive = _SESSIONS.get(handle)
    if archive is None:
        known = ", ".join(_SESSIONS) or "нет открытых архивов"
        raise FscpError(f"неизвестный handle '{handle}' ({known}); вызовите fscp_open")
    if not archive.path.exists() and not archive.dirty:
        raise FscpError(f"файл {archive.path} исчез; откройте архив заново")
    if archive.path.stat().st_mtime != archive.mtime and not archive.dirty:
        raise FscpError(
            f"{archive.path.name} изменился на диске; вызовите fscp_open заново"
        )
    return archive


def close(handle: str) -> bool:
    return _SESSIONS.pop(handle, None) is not None


def sessions() -> dict[str, FscpArchive]:
    return dict(_SESSIONS)
