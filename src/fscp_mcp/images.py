"""Подложки планов — записи Content/<guid> внутри архива.

Файлы лежат без расширения, поэтому тип определяется по сигнатуре, а не по
имени: в реальных конфигурациях встречаются и PNG, и JPEG. Размеры читаются из
заголовка (IHDR у PNG, маркер SOF у JPEG) — распаковывать картинку целиком ради
ширины и высоты не нужно, а оригиналы бывают по 5000 пикселей.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

#: Сколько байт хватает, чтобы определить тип и размеры.
#: PNG укладывается в 24, JPEG требует дойти до маркера SOF.
HEAD_BYTES = 4096

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (PNG_MAGIC, "image/png"),
    (JPEG_MAGIC, "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

#: Маркеры SOF: несут размеры кадра. SOF4/SOF8/SOF12 (c4/c8/cc) — не кадровые.
_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


@dataclass(frozen=True, slots=True)
class ImageInfo:
    guid: str
    entry: str
    size_bytes: int
    media_type: str
    width: int | None
    height: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "guid": self.guid,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "width": self.width,
            "height": self.height,
        }


def sniff(head: bytes) -> str:
    for magic, media_type in _SIGNATURES:
        if head.startswith(magic):
            return media_type
    stripped = head.lstrip()
    if stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
        return "image/svg+xml"
    return "application/octet-stream"


def png_dimensions(head: bytes) -> tuple[int, int] | None:
    """Ширина и высота из чанка IHDR (первые 24 байта файла)."""
    if not head.startswith(PNG_MAGIC) or len(head) < 24:
        return None
    if head[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def jpeg_dimensions(head: bytes) -> tuple[int, int] | None:
    """Ширина и высота из первого кадрового маркера SOF."""
    if not head.startswith(JPEG_MAGIC):
        return None
    offset = 2
    limit = len(head)
    while offset + 9 < limit:
        if head[offset] != 0xFF:
            offset += 1
            continue
        marker = head[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        segment_length = int.from_bytes(head[offset + 2 : offset + 4], "big")
        if segment_length < 2:
            return None
        if marker in _SOF_MARKERS:
            height, width = struct.unpack(">HH", head[offset + 5 : offset + 9])
            return width, height
        offset += 2 + segment_length
    return None


def dimensions(head: bytes) -> tuple[int, int] | None:
    return png_dimensions(head) or jpeg_dimensions(head)


def describe(guid: str, entry: str, size_bytes: int, head: bytes) -> ImageInfo:
    size = dimensions(head)
    return ImageInfo(
        guid=guid,
        entry=entry,
        size_bytes=size_bytes,
        media_type=sniff(head),
        width=size[0] if size else None,
        height=size[1] if size else None,
    )
