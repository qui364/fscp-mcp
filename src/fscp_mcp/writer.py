"""Сериализация дерева ElementTree обратно в формат .NET XmlSerializer.

Global Monitor читает файлы, которые сам же и записал, и насколько придирчиво -
неизвестно. Поэтому требование к этому модулю жёстче обычного: разобранный и
записанный обратно файл обязан совпасть с исходным **побайтово**. Тогда любое
расхождение в открытом файле - это правка, а не артефакт сериализации.
Проверяется на реальных конфигурациях в tests/test_real_configs.py.

Что именно воспроизводится (снято с рабочих конфигураций):

* декларация ровно ``<?xml version="1.0"?>`` - без encoding и standalone;
* отступ 2 пробела, пустой элемент - ``<Foo />`` с пробелом перед слешем;
* перевод строки **зависит от записи**: обычно CRLF, но встречается и LF,
  поэтому берётся из исходных байтов, а не выбирается;
* объявления пространств имён на корне сохраняются дословной строкой: у
  GKDeviceConfiguration это xmlns:xsd + xmlns:xsi, у PlansConfiguration
  бывает только xmlns:xsi. ElementTree их теряет, если префикс не использован.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from .errors import FscpError

DECLARATION = '<?xml version="1.0"?>'
CRLF = "\r\n"
INDENT = "  "

#: Обратный перевод Clark-нотации ElementTree в префиксы, которыми пишет .NET.
PREFIXES = {
    "http://www.w3.org/2001/XMLSchema-instance": "xsi",
    "http://www.w3.org/2001/XMLSchema": "xsd",
}

#: Строка xmlns-атрибутов корня: её надо перенести в вывод как есть.
_ROOT_ATTRS = re.compile(r"<[A-Za-z_][\w.-]*((?:\s+xmlns:[\w.-]+=\"[^\"]*\")*)\s*/?>")


def escape_text(value: str) -> str:
    """Экранирование текстового узла так, как это делает XmlTextWriter."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\r", "&#xD;")
    )


def escape_attr(value: str) -> str:
    """Экранирование значения атрибута: кавычка и все переводы строк."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
        .replace("\r", "&#xD;")
        .replace("\n", "&#xA;")
        .replace("\t", "&#x9;")
    )


def qname(tag: str) -> str:
    """'{http://...}nil' -> 'xsi:nil'."""
    if not tag.startswith("{"):
        return tag
    uri, _, local = tag[1:].partition("}")
    prefix = PREFIXES.get(uri)
    if prefix is None:
        raise FscpError(f"неизвестное пространство имён в атрибуте: {uri}")
    return f"{prefix}:{local}"


def root_header(raw: bytes) -> tuple[str, str]:
    """Из исходных байтов записи - xmlns-строка корня и перевод строки.

    Оба нужны для побайтового round-trip и оба не восстанавливаются из дерева.
    """
    newline = "\r\n" if b"?>\r\n" in raw[:64] else "\n"
    head = raw[:2048].decode("utf-8", errors="replace")
    _, _, after = head.partition("?>")
    match = _ROOT_ATTRS.search(after)
    return (match.group(1) if match else ""), newline


def serialize(
    root: ET.Element, *, root_attrs: str = "", newline: str = "\r\n"
) -> bytes:
    """Дерево -> байты в формате XmlSerializer."""
    lines: list[str] = [DECLARATION]
    _write(root, lines, 0, root_attrs)
    return newline.join(lines).encode("utf-8")


def _write(element: ET.Element, lines: list[str], depth: int, extra: str) -> None:
    pad = INDENT * depth
    attrs = extra
    for key, value in element.attrib.items():
        attrs += f' {qname(key)}="{escape_attr(value)}"'

    children = list(element)
    text = element.text or ""

    if children:
        if text.strip():
            raise FscpError(
                f"смешанное содержимое в <{element.tag}> - формат такого не "
                "использует, сериализатор потерял бы текст"
            )
        lines.append(f"{pad}<{element.tag}{attrs}>")
        for child in children:
            _write(child, lines, depth + 1, "")
        lines.append(f"{pad}</{element.tag}>")
    elif text.strip():
        lines.append(f"{pad}<{element.tag}{attrs}>{escape_text(text)}</{element.tag}>")
    else:
        lines.append(f"{pad}<{element.tag}{attrs} />")
