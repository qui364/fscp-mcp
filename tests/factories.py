"""Сборка синтетической конфигурации .fscp для тестов.

Реальные конфигурации в репозиторий не выкладываются: в них данные объекта и
SecurityConfiguration.xml с хешами паролей. Поэтому базовые тесты гоняются на
архиве, который собирается здесь — по образцу настоящего файла: тот же набор
записей ZIP в том же порядке, тот же выхлоп XmlSerializer и те же GUID драйверов
из drivers.json.

Дерево устройств подобрано так, чтобы задеть все четыре правила адресации из
FscpArchive._address_for: сквозную нумерацию стволов, no_address-контейнер,
сквозной групповой узел и обычное дописывание IntAddress.

Ожидаемые значения экспортируются константами — тесты сверяются с ними, а не с
цифрами, списанными с чужой конфигурации.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from fscp_mcp import writer

# --------------------------------------------------------------- драйверы

LOCAL_NET = "938947c5-4624-4a1a-939c-60aeebf7b65c"
RSGK = "7aa244a1-bf4c-4b4b-85c7-d9e53df3071a"
GK = "c052395d-043f-4590-a0b8-bc49867adc6a"  # no_address
KAU = "57c45124-9300-49bc-a268-68f3d929927b"
ALS = "4d5647df-a278-48f6-9f89-19e4d51b7711"
IP212 = "a50ffa41-b53e-4b3b-addf-cdbba631feb2"  # ИП 212-149
AM4 = "79eac50a-d534-4775-a102-be4872877400"  # is_group
RELAY_GROUP = "77980273-4b1d-4acc-915e-95ffdcd1dd02"  # Группа реле, no_address

# ------------------------------------------------------------------- UID

NIL = "00000000-0000-0000-0000-000000000000"

D_ROOT = "11111111-0000-4000-8000-000000000001"
D_RSGK1 = "11111111-0000-4000-8000-000000000002"
D_GK1 = "11111111-0000-4000-8000-000000000003"
D_KAU1 = "11111111-0000-4000-8000-000000000004"
D_ALS1 = "11111111-0000-4000-8000-000000000005"
D_IP1 = "11111111-0000-4000-8000-000000000006"
D_IP2 = "11111111-0000-4000-8000-000000000007"
D_AM4 = "11111111-0000-4000-8000-000000000008"
D_AM4_A = "11111111-0000-4000-8000-000000000009"
D_AM4_B = "11111111-0000-4000-8000-00000000000a"
D_RELAYS = "11111111-0000-4000-8000-00000000000b"
D_RELAY_A = "11111111-0000-4000-8000-00000000000c"
D_RSGK2 = "11111111-0000-4000-8000-00000000000d"
D_GK2 = "11111111-0000-4000-8000-00000000000e"
D_KAU2 = "11111111-0000-4000-8000-00000000000f"
D_ALS2 = "11111111-0000-4000-8000-000000000010"
D_IP3 = "11111111-0000-4000-8000-000000000011"

ZONE1 = "22222222-0000-4000-8000-000000000001"
ZONE2 = "22222222-0000-4000-8000-000000000002"
SCENARIO1 = "22222222-0000-4000-8000-000000000003"
DIRECTION1 = "22222222-0000-4000-8000-000000000004"
TEMPLATE = "22222222-0000-4000-8000-000000000005"
TEMPLATE_DEVICE = "22222222-0000-4000-8000-000000000006"

PLAN_ROOT = "33333333-0000-4000-8000-000000000001"
PLAN_NESTED = "33333333-0000-4000-8000-000000000002"

PNG_GUID = "44444444-0000-4000-8000-000000000001"
JPEG_GUID = "44444444-0000-4000-8000-000000000002"

#: Иконка, зашитая в Global Monitor: в архиве её нет и быть не должно.
APP_RESOURCE = "GKModule/Images/Zone.png"

# ------------------------------------------------------- ожидаемые значения

GK_VERSION = "2.9"
PLANS_VERSION = "2.6"

#: Все узлы дерева, включая RootDevice.
EXPECTED_DEVICES = 17
EXPECTED_GK = 2
EXPECTED_ZONES = 2

#: АЛС, на которую вешаются приборы: сюда добавляют устройства тесты записи.
LINE_ADDRESS = "1.2.1"
#: АЛС второго ствола - цель для проверки переноса между линиями.
SECOND_LINE_ADDRESS = "2.1.1"

IP_ADDRESS = "1.2.1.1"
IP_DRIVER = "ИП 212-149"
IP_NAME = "ИП 212-149 1.2.1.1"
IP_PROPERTY = ("Порог запыленности, дБ/м", "90")

#: Description дописывается к имени без пробела.
DESCRIBED_ADDRESS = "1.2.1.2"
DESCRIBED_NAME = "ИП 212-149 1.2.1.2(Холл)"

#: Дети сквозного группового узла стоят с ним на одном уровне: 1.2.1.3 -> 1.2.1.4.
GROUP_ADDRESS = "1.2.1.3"
GROUP_CHILD_ADDRESS = "1.2.1.4"

#: no_address-контейнер показывает адрес родителя, его ребёнок встаёт на уровень
#: самого контейнера.
RELAYS_ADDRESS = "1.2.1"
RELAY_CHILD_ADDRESS = "1.2.7"

GK_IPS = ("172.16.5.11", "172.16.5.12")
SECOND_TRUNK_ADDRESS = "2.1.1.1"

ZONE_NAMES = ("1.Склад", "2.Коридор")
SCENARIO_NAME = "1.ЛИФТЫ"
SCENARIO_LOGIC = "Если Пожар2 в любой из зон: 1.Склад, 2.Коридор"

PLAN_NAME = "Этаж 1"
NESTED_PLAN_NAME = "Серверная"

#: Подпись объекта на плане кэшируется при отрисовке; эта намеренно устаревшая —
#: на ней проверяется детектор в validate_config.
STALE_LABEL = "ИП 212-149 1.2.1.9"

PNG_SIZE = (4, 3)
JPEG_SIZE = (640, 480)


# --------------------------------------------------------------- части XML


def _property(name: str, value: str, string: bool = False) -> str:
    tag = "StringValue" if string else "Value"
    return f"<GKProperty><Name>{name}</Name><{tag}>{value}</{tag}></GKProperty>"


def _device(
    uid: str,
    driver: str,
    int_address: int,
    children: str = "",
    properties: str = "",
    zones: tuple[str, ...] = (),
    description: str = "",
) -> str:
    zone_uids = "".join(f"<guid>{z}</guid>" for z in zones)
    described = f"<Description>{description}</Description>" if description else ""
    # Порядок полей сверен с рабочими конфигурациями (schema.DEVICE_FIELDS):
    # Description идёт сразу за No, а не перед Children. XmlSerializer на
    # чужом порядке молча теряет поле, и validate_config это ловит.
    return f"""<GKDevice>
      <UID>{uid}</UID>
      <No>0</No>
      {described}
      <AllowMultipleVisualization>false</AllowMultipleVisualization>
      <PlanElementUIDs />
      <IsDisabled>false</IsDisabled>
      <ReserveGkUID>{NIL}</ReserveGkUID>
      <DriverUID>{driver}</DriverUID>
      <IsInnerKau>false</IsInnerKau>
      <IntAddress>{int_address}</IntAddress>
      <Children>{children}</Children>
      <Properties>{properties}</Properties>
      <DeviceProperties />
      <ZoneUIDs>{zone_uids}</ZoneUIDs>
      <GuardZoneUIDs />
      <Logic>
        <OnClausesGroup>
          <PimUID>{NIL}</PimUID>
          <ClauseGroups />
          <Clauses />
          <CardClauses />
          <ClauseJoinOperationType>Or</ClauseJoinOperationType>
          <ForceLogicOnKAU>false</ForceLogicOnKAU>
        </OnClausesGroup>
      </Logic>
      <SerialNo />
    </GKDevice>"""


def _gk_config() -> bytes:
    """GKDeviceConfiguration.xml: дерево устройств, зоны, сценарий, шаблон."""
    relays = _device(D_RELAYS, RELAY_GROUP, 6, children=_device(D_RELAY_A, IP212, 7))
    group = _device(
        D_AM4, AM4, 3, children=_device(D_AM4_A, IP212, 4) + _device(D_AM4_B, IP212, 5)
    )
    als1 = _device(
        D_ALS1,
        ALS,
        1,
        children=(
            _device(
                D_IP1,
                IP212,
                1,
                properties=_property(*IP_PROPERTY),
                zones=(ZONE1, ZONE2),
            )
            + _device(D_IP2, IP212, 2, description="Холл", zones=(ZONE1,))
            + group
            + relays
        ),
    )
    # ГК и КАУ — соседи под РСГК: ГК своего адреса не имеет, нумерацию линий
    # ведёт РСГК.
    trunk1 = _device(
        D_RSGK1,
        RSGK,
        1,
        children=(
            _device(
                D_GK1,
                GK,
                0,
                properties=_property("IPAddress", GK_IPS[0], string=True),
            )
            + _device(D_KAU1, KAU, 2, children=als1)
        ),
    )
    trunk2 = _device(
        D_RSGK2,
        RSGK,
        2,
        children=(
            _device(
                D_GK2,
                GK,
                0,
                properties=_property("IPAddress", GK_IPS[1], string=True),
            )
            + _device(
                D_KAU2,
                KAU,
                1,
                children=_device(D_ALS2, ALS, 1, children=_device(D_IP3, IP212, 1)),
            )
        ),
    )

    zones = "".join(
        f"""<GKZone>
      <UID>{uid}</UID>
      <No>{no}</No>
      <Name>{name}</Name>
      <AllowMultipleVisualization>false</AllowMultipleVisualization>
      <PlanElementUIDs />
      <IsFireB>false</IsFireB>
      <FireBDelayTime>30</FireBDelayTime>
      <Fire1Count>1</Fire1Count>
      <Fire2Count>2</Fire2Count>
    </GKZone>"""
        for uid, no, name in (
            (ZONE1, 1, ZONE_NAMES[0].partition(".")[2]),
            (ZONE2, 2, ZONE_NAMES[1].partition(".")[2]),
        )
    )

    delay = f"""<GKDelay>
      <UID>{SCENARIO1}</UID>
      <No>{SCENARIO_NAME.partition(".")[0]}</No>
      <Name>{SCENARIO_NAME.partition(".")[2]}</Name>
      <PlanElementUIDs />
      <DelayTime>0</DelayTime>
      <Hold>0</Hold>
      <DelayRegime>On</DelayRegime>
      <Logic>
        <OnClausesGroup>
          <PimUID>{NIL}</PimUID>
          <ClauseGroups />
          <Clauses>
            <GKClause>
              <ClauseConditionType>If</ClauseConditionType>
              <StateType>Fire2</StateType>
              <DeviceUIDs />
              <ZoneUIDs>
                <guid>{ZONE1}</guid>
                <guid>{ZONE2}</guid>
              </ZoneUIDs>
              <GuardZoneUIDs />
              <DirectionUIDs />
              <DelayUIDs />
              <DoorUIDs />
              <MPTUIDs />
              <PumpStationsUIDs />
              <ClauseOperationType>AnyZone</ClauseOperationType>
            </GKClause>
          </Clauses>
          <CardClauses />
          <ClauseJoinOperationType>Or</ClauseJoinOperationType>
          <ForceLogicOnKAU>false</ForceLogicOnKAU>
        </OnClausesGroup>
      </Logic>
      <DirectionDevices>
        <DirectionDevice>
          <DeviceUid>{D_AM4_A}</DeviceUid>
          <CommandType>TurnOn_InManual</CommandType>
        </DirectionDevice>
      </DirectionDevices>
      <IsAutoGenerated>false</IsAutoGenerated>
      <IsAutoReset>false</IsAutoReset>
      <PumpStationUID>{NIL}</PumpStationUID>
      <DoorUID>{NIL}</DoorUID>
    </GKDelay>"""

    direction = f"""<GKDirection>
      <UID>{DIRECTION1}</UID>
      <No>1</No>
      <Name>Оповещение</Name>
      <PlanElementUIDs />
      <Delay>0</Delay>
      <Hold>0</Hold>
    </GKDirection>"""

    # В шаблоне параметров лежит GKDevice — он не часть дерева устройств.
    template = f"""<GKParameterTemplate>
      <UID>{TEMPLATE}</UID>
      <No>0</No>
      <Name>По умолчанию</Name>
      <DeviceParameterTemplates>
        <GKDeviceParameterTemplate>
          {_device(TEMPLATE_DEVICE, GK, 0, properties=_property("Яркость, %", "50"))}
        </GKDeviceParameterTemplate>
      </DeviceParameterTemplates>
    </GKParameterTemplate>"""

    major, minor = GK_VERSION.split(".")
    xml = f"""<?xml version="1.0"?>
<GKDeviceConfiguration xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Version>
    <MajorVersion>{major}</MajorVersion>
    <MinorVersion>{minor}</MinorVersion>
  </Version>
  <RootDevice>
    <UID>{D_ROOT}</UID>
    <No>0</No>
    <AllowMultipleVisualization>false</AllowMultipleVisualization>
    <PlanElementUIDs />
    <IsDisabled>false</IsDisabled>
    <ReserveGkUID>{NIL}</ReserveGkUID>
    <DriverUID>{LOCAL_NET}</DriverUID>
    <IsInnerKau>false</IsInnerKau>
    <IntAddress>0</IntAddress>
    <Children>{trunk1}{trunk2}</Children>
    <Properties />
    <DeviceProperties />
    <ZoneUIDs />
    <GuardZoneUIDs />
  </RootDevice>
  <Zones>{zones}</Zones>
  <Directions>{direction}</Directions>
  <PumpStations />
  <MPTs />
  <Delays>{delay}</Delays>
  <GuardZones />
  <Doors />
  <SKDZones />
  <ParameterTemplates>{template}</ParameterTemplates>
</GKDeviceConfiguration>"""
    return _canonical(xml)


def _point_object(
    uid: str, item_uid: str, object_name: str, label: str, image: str = ""
) -> str:
    source = (
        f"<BackgroundImageSource>{image}</BackgroundImageSource>"
        if image
        else '<BackgroundImageSource xsi:nil="true" />'
    )
    return f"""<PointObject>
          <UID>{uid}</UID>
          <No>0</No>
          <Name>{label}</Name>
          {source}
          <BackgroundSVGImageSource xsi:nil="true" />
          <ImageType>Image</ImageType>
          <ZIndex>0</ZIndex>
          <ZLayer>70</ZLayer>
          <Left>47.06</Left>
          <Top>84.84</Top>
          <ItemUID>{item_uid}</ItemUID>
          <ModuleName>Устройства Рубеж</ModuleName>
          <ObjectName>{object_name}</ObjectName>
        </PointObject>"""


def _plans_config() -> bytes:
    """PlansConfiguration.xml: корневой план с вложенным, объекты и подложки."""
    nested = f"""<Plan>
        <UID>{PLAN_NESTED}</UID>
        <No>1</No>
        <Name>{NESTED_PLAN_NAME}</Name>
        <BackgroundImageSource>{JPEG_GUID}</BackgroundImageSource>
        <BackgroundSVGImageSource xsi:nil="true" />
        <Children />
        <Height>210</Height>
        <ImageType>Image</ImageType>
        <PointObjects>
          {_point_object(
              "55555555-0000-4000-8000-000000000004",
              D_AM4,
              "GKDevice",
              f"АМ4 {GROUP_ADDRESS}",
          )}
        </PointObjects>
        <Width>297</Width>
      </Plan>"""

    objects = "".join(
        (
            _point_object("55555555-0000-4000-8000-000000000001", D_IP1, "GKDevice", IP_NAME),
            _point_object("55555555-0000-4000-8000-000000000002", D_IP2, "GKDevice", STALE_LABEL),
            _point_object(
                "55555555-0000-4000-8000-000000000003",
                ZONE1,
                "GKZone",
                ZONE_NAMES[0],
                image=APP_RESOURCE,
            ),
        )
    )

    major, minor = PLANS_VERSION.split(".")
    xml = f"""<?xml version="1.0"?>
<PlansConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Version>
    <MajorVersion>{major}</MajorVersion>
    <MinorVersion>{minor}</MinorVersion>
  </Version>
  <CataloguePlans />
  <CatalogueStructure>
    <Catalogues />
  </CatalogueStructure>
  <Plans>
    <Plan>
      <UID>{PLAN_ROOT}</UID>
      <No>0</No>
      <Name>{PLAN_NAME}</Name>
      <BackgroundImageSource>{PNG_GUID}</BackgroundImageSource>
      <BackgroundSVGImageSource xsi:nil="true" />
      <Children>{nested}</Children>
      <Height>210</Height>
      <ImageType>Image</ImageType>
      <PointObjects>{objects}</PointObjects>
      <Width>297</Width>
    </Plan>
  </Plans>
</PlansConfiguration>"""
    return _canonical(xml)


# ----------------------------------------------------------------- подложки


def png(width: int = PNG_SIZE[0], height: int = PNG_SIZE[1]) -> bytes:
    """Настоящий PNG: размеры из IHDR должны сойтись с тем, что видит декодер."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg() -> bytes:
    """JPEG из одних заголовков: SOI + APP0 + SOF0 с кадром 640x480.

    Пикселей в нём нет — размеры читаются из маркера SOF, декодировать файл ради
    ширины и высоты не нужно.
    """
    return bytes.fromhex(
        "ffd8"
        "ffe000104a46494600010100000100010000"
        "ffc000110801e0028003012200021101031101"
    )


# ------------------------------------------------------------------ сборка


def _canonical(xml: str) -> bytes:
    """Приводит рукописный шаблон к раскладке .NET XmlSerializer.

    Шаблоны выше задают **содержимое** - какие элементы, в каком порядке и с
    какими значениями. Раскладку (отступ по глубине, каждый элемент на своей
    строке, пустой как <Foo />) задаёт сериализатор: расписывать её в f-строках
    для дерева глубиной в пять уровней нечитаемо и всё равно разъедется.

    Что сериализатор кладёт байт-в-байт как настоящий Global Monitor,
    проверяется не здесь, а в test_real_configs.py на рабочих конфигурациях -
    синтетика для такой проверки не источник истины.
    """
    return writer.serialize(ET.fromstring(xml), root_attrs=writer.root_header(
        xml.encode("utf-8")
    )[0], newline="\n")


#: Реальные конфигурации почти всегда CRLF, поэтому синтетика по умолчанию
#: тоже: иначе путь CRLF в сериализаторе не проверялся бы вообще - реальные
#: конфигурации в CI не идут.
DEFAULT_NEWLINE = "\r\n"

#: Собственный таймстамп блоба Content/: в настоящем архиве подложки хранят
#: время своей загрузки, а не время сохранения конфигурации, и запись обязана
#: его сохранять.
BLOB_DATE_TIME = (2025, 11, 13, 12, 58, 22)
CONFIG_DATE_TIME = (2026, 8, 20, 20, 44, 10)


def _eol(data: bytes, newline: str) -> bytes:
    """Переводы строк в шаблонах записаны как \n - приводим к нужным."""
    if newline == "\n":
        return data
    return data.replace(b"\n", newline.encode("utf-8"))


def build(path: Path, newline: str = DEFAULT_NEWLINE) -> Path:
    """Полная конфигурация: тот же набор записей, что и у настоящего файла."""

    def entry(name: str, date_time: tuple[int, ...] = CONFIG_DATE_TIME) -> ZipInfo:
        info = ZipInfo(name, date_time=date_time)
        info.compress_type = ZIP_STORED if name.endswith("/") else ZIP_DEFLATED
        return info

    layouts = b'<?xml version="1.0"?>\n<LayoutsConfiguration />'
    security = b'<?xml version="1.0"?>\n<SecurityConfiguration />'
    system = b'<?xml version="1.0"?>\n<SystemConfiguration />'

    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(entry("Content/"), b"")
        archive.writestr(entry("GKDeviceConfiguration.xml"), _eol(_gk_config(), newline))
        archive.writestr(entry("LayoutsConfiguration.xml"), _eol(layouts, newline))
        archive.writestr(entry("PlansConfiguration.xml"), _eol(_plans_config(), newline))
        archive.writestr(entry("Resources/"), b"")
        # Содержимого не пишем: сервер эту запись намеренно не открывает.
        archive.writestr(entry("SecurityConfiguration.xml"), _eol(security, newline))
        archive.writestr(entry("SystemConfiguration.xml"), _eol(system, newline))
        archive.writestr(entry(f"Content/{PNG_GUID}", BLOB_DATE_TIME), png())
        archive.writestr(entry(f"Content/{JPEG_GUID}", BLOB_DATE_TIME), jpeg())
    return path


def build_empty(path: Path) -> Path:
    path.write_bytes(b"")
    return path


def build_garbage(path: Path) -> Path:
    path.write_bytes(b"not a zip at all")
    return path


def build_without_gk_config(path: Path) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("PlansConfiguration.xml", _plans_config())
    return path
