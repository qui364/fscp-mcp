"""Сборка новой пустой конфигурации .fscp.

Пустой GKDeviceConfiguration - это 33 элемента верхнего уровня, включая
OPCSettings и настройки цветов охранных зон. Выдумывать их нельзя: значения
задаёт сама программа, а не мы. Поэтому шаблон снят с рабочей конфигурации
скриптом tools/make_skeleton.py, обезличен (в нём остались только GUID'ы,
числа, true/false и ключевые слова перечислений) и лежит рядом ресурсными
файлами - ровно в той байтовой раскладке, в какой их пишет Global Monitor.

Файлами, а не f-строками в модуле: шаблон - это **наблюдённая** раскладка, и
её надо уметь сверить побайтово. Модуль соблазняет «причесать форматирование»,
и файл молча разойдётся с тем, что читает программа.

SecurityConfiguration.xml не создаётся: читать его нельзя, а выдумывать хеши
паролей - тем более. Либо пользователь указывает донора, и запись переносится
байтами, либо её в новом файле нет и учётные записи заводятся в самом
Global Monitor.
"""

from __future__ import annotations

import re
import zipfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .errors import FscpError

SKELETON = Path(__file__).resolve().parent / "skeleton"

#: Записи нового архива в том порядке, в каком их пишет Global Monitor.
LAYOUT = (
    "Content/",
    "GKDeviceConfiguration.xml",
    "LayoutsConfiguration.xml",
    "PlansConfiguration.xml",
    "Resources/",
    "SecurityConfiguration.xml",
    "SystemConfiguration.xml",
)

TEMPLATES = (
    "GKDeviceConfiguration.xml",
    "LayoutsConfiguration.xml",
    "PlansConfiguration.xml",
    "SystemConfiguration.xml",
)

SECURITY_CONFIG = "SecurityConfiguration.xml"

#: Плейсхолдеры UID в шаблоне. Каждый заменяется своим свежим uuid4: тащить
#: UID корня из чужой конфигурации незачем.
PLACEHOLDER = re.compile(r"\{uid(\d+)\}")

#: Внешние атрибуты записи. Ноль - то, что пишут свежие версии программы.
EXTERNAL_ATTR = 0


def render(name: str, file_name: str) -> bytes:
    """Шаблон с подставленными свежими UID и именем файла."""
    source = SKELETON / name
    if not source.is_file():
        raise FscpError(
            f"шаблон {name} не найден в {SKELETON}; пакет собран без ресурсов "
            "skeleton/*.xml"
        )
    text = source.read_text(encoding="utf-8", newline="")

    fresh: dict[str, str] = {}

    def swap(match: re.Match[str]) -> str:
        key = match.group(0)
        if key not in fresh:
            fresh[key] = str(uuid4())
        return fresh[key]

    text = PLACEHOLDER.sub(swap, text)
    # Имя файла Global Monitor пишет внутрь конфигурации. Не подставив своё,
    # мы оставили бы в новом файле чужое.
    text = text.replace("{file_name}", file_name)
    return text.encode("utf-8")


def create(target: Path, donor: Path | None = None) -> dict[str, object]:
    """Собирает новый .fscp по шаблону.

    donor - существующая конфигурация, из которой побайтово переносится
    SecurityConfiguration.xml. Она не разбирается и наружу не отдаётся: нам
    нужны только её байты, чтобы у нового файла были учётные записи.
    """
    security: bytes | None = None
    if donor is not None:
        if not donor.is_file():
            raise FscpError(f"донор не найден: {donor}")
        try:
            with zipfile.ZipFile(donor) as source:
                if SECURITY_CONFIG not in source.namelist():
                    raise FscpError(
                        f"в доноре {donor.name} нет {SECURITY_CONFIG} - "
                        "переносить нечего"
                    )
                security = source.read(SECURITY_CONFIG)
        except zipfile.BadZipFile as exc:
            raise FscpError(f"{donor.name}: это не архив .fscp") from exc

    stamp = datetime.now().timetuple()[:6]
    temporary = target.with_name(target.name + ".tmp")
    written: list[str] = []

    try:
        with zipfile.ZipFile(temporary, "w") as out:
            for name in LAYOUT:
                if name == SECURITY_CONFIG and security is None:
                    continue
                info = zipfile.ZipInfo(name, date_time=stamp)
                info.compress_type = (
                    zipfile.ZIP_STORED if name.endswith("/") else zipfile.ZIP_DEFLATED
                )
                if name.endswith("/"):
                    payload = b""
                elif name == SECURITY_CONFIG:
                    payload = security or b""
                else:
                    payload = render(name, target.name)
                out.writestr(info, payload)
                # zipfile подменяет нулевой external_attr - возвращаем на место
                # до того, как запишется центральный каталог.
                info.external_attr = EXTERNAL_ATTR
                written.append(name)
        temporary.replace(target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise FscpError(f"не удалось записать {target}: {exc}") from exc

    return {
        "path": str(target),
        "entries": written,
        "security": (
            f"перенесён из {donor.name}" if security is not None else "нет записи"
        ),
    }
