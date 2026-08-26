"""Точка входа: MCP-сервер поверх stdio.

Кириллица в JSON-RPC и в логах требует явного UTF-8: консоль Windows по
умолчанию отдаёт cp1251 и превращает русские имена в mojibake.
"""

from __future__ import annotations

import sys


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    from .server import server

    server.run("stdio")


if __name__ == "__main__":
    main()
