from __future__ import annotations

import os
from pathlib import Path

import pytest

from fscp_mcp import archive

from . import factories

#: Каталог с реальными конфигурациями. Они содержат данные объекта и в
#: репозиторий не выкладываются, поэтому путь задаётся снаружи; без переменной
#: проверки по ним пропускаются, а базовые тесты идут на синтетике.
CONFIGS_ENV = "FSCP_TEST_CONFIGS"


def real_configs() -> list[Path]:
    raw = os.environ.get(CONFIGS_ENV, "").strip().strip('"')
    if not raw:
        return []
    directory = Path(raw).expanduser()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.fscp"))


def openable_configs() -> list[Path]:
    """Только те, что вообще открываются: битые файлы держат и там."""
    out = []
    for path in real_configs():
        try:
            archive.open_archive(path)
        except archive.FscpError:
            continue
        out.append(path)
    return out


@pytest.fixture(scope="session")
def synthetic_path(tmp_path_factory) -> Path:
    return factories.build(tmp_path_factory.mktemp("fscp") / "синтетика.fscp")


@pytest.fixture(scope="session")
def synthetic(synthetic_path) -> archive.FscpArchive:
    _, parsed = archive.open_archive(synthetic_path)
    return parsed


@pytest.fixture
def handle(synthetic_path) -> str:
    """Открывается заново на каждый тест: сессионный кэш держит не больше
    MAX_SESSIONS архивов и мог вытеснить синтетику."""
    opened, _ = archive.open_archive(synthetic_path)
    return opened


@pytest.fixture(scope="session")
def anyio_backend():
    """Плагин anyio из зависимостей mcp гоняет async-тесты; хватает asyncio."""
    return "asyncio"
