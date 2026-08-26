# fscp-mcp

[![tests](https://github.com/qui364/fscp-mcp/actions/workflows/tests.yml/badge.svg)](https://github.com/qui364/fscp-mcp/actions/workflows/tests.yml)

MCP-сервер для чтения конфигураций `.fscp` — файлов системы противопожарной
защиты «Рубеж-Глобал» (Windows Global Monitor).

Конфигурация реального объекта — это 25 МБ XML, 512 тыс. строк и 5656
устройств, у которых нет имён: только GUID, `DriverUID` и `IntAddress`. Ни
открыть целиком, ни осмысленно грепнуть такое нельзя. Сервер разбирает архив
один раз и отвечает адресуемыми страницами: «что стоит на КАУ 1.2», «что за
объект `84ee9eae-…`», «какая логика у сценария ЛИФТЫ», «покажи подложку плана».

Версия 1 — только чтение.

## Установка

Нужен Python 3.11 или новее. Ставится одной командой — клонировать репозиторий
для этого не требуется:

```bash
uv tool install "git+https://github.com/qui364/fscp-mcp"
```

Без `uv` то же самое делает `pipx install "git+https://github.com/qui364/fscp-mcp"`,
а в обычное окружение — `pip install "git+https://github.com/qui364/fscp-mcp"`.
После установки появляется команда `fscp-mcp` — это и есть сервер, он говорит по
stdio и запускается не руками, а клиентом.

Extra `img` (Pillow) нужен только для инлайновых превью подложек — без него
работает всё, кроме `get_plan_image`:

```bash
uv tool install "fscp-mcp[img] @ git+https://github.com/qui364/fscp-mcp"
```

### Claude Desktop

Настройки → Developer → Edit Config открывает
`claude_desktop_config.json` (Windows: `%APPDATA%\Claude`, macOS:
`~/Library/Application Support/Claude`). Добавьте в него сервер, сохранив то,
что уже есть в файле:

```json
{
  "mcpServers": {
    "fscp": {
      "command": "fscp-mcp",
      "env": { "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

Если `fscp-mcp` не виден приложению (частый случай на Windows: PATH у GUI свой),
укажите полный путь к нему — `uv tool list` или `pipx list` покажут, куда он
установлен. `env` не декоративный: без него консоль Windows отдаёт cp1251 и
кириллица в ответах превращается в mojibake.

Дальше перезапустите Claude Desktop — сервер появится в новом чате.

### Claude Code

В клонированном репозитории сервер поднимется сам: конфигурация лежит в
`.mcp.json`. Чтобы он был доступен в любом проекте, зарегистрируйте его глобально:

```bash
claude mcp add fscp --scope user -- fscp-mcp
```

### Разработка

```bash
py -3 -m venv .venv
```

```bash
.venv/Scripts/python.exe -m pip install -e ".[dev,img]"
```

```bash
.venv/Scripts/python.exe -m pytest -q
```

Под Windows в `PATH` обычно висит заглушка `python` из `WindowsApps` — она не
работает, интерпретатор вызывайте явным путём.

## С чего начать

Скажите модели, какой файл открыть, — дальше она работает по `handle`:

> открой C:\Конфигурации\объект.fscp и покажи, что стоит на КАУ 1.2

Полезно знать: конфигурация читается только на чтение, а
`SecurityConfiguration.xml` (хеши паролей пользователей) сервер не открывает
вообще.

## Тесты

Базовые тесты не требуют ничего, кроме репозитория: конфигурация для них
собирается на лету в `tests/factories.py` — ZIP с теми же записями и тем же
выхлопом `XmlSerializer`, что у настоящего файла.

Реальные конфигурации не публикуются — в них данные объекта и
`SecurityConfiguration.xml` с хешами паролей. Если они есть локально, укажите
каталог, и добавится сверка адресов с подписями на планах (порог 93 %),
проверка скорости разбора 25 МБ и детектор устаревших подписей:

```bash
FSCP_TEST_CONFIGS=<каталог с .fscp> .venv/Scripts/python.exe -m pytest -q
```

## Инструменты

| Группа | Инструменты |
|---|---|
| Сессия | `fscp_open`, `fscp_close`, `fscp_info` |
| Устройства | `list_devices`, `get_device`, `search_devices`, `device_tree` |
| Объекты ГК | `list_objects`, `get_object`, `resolve_uid` |
| Планы | `list_plans`, `get_plan`, `find_on_plans` |
| Подложки | `list_plan_images`, `extract_plan_image`, `get_plan_image` |
| Прочее | `list_drivers`, `read_xml`, `export_devices_csv`, `validate_config` |

Устройство адресуется либо UID, либо адресом вида `1.2.1.1`.

## Что внутри .fscp

ZIP с XML от .NET `XmlSerializer` плюс подложки планов в `Content/`.
В охвате `GKDeviceConfiguration.xml`, `PlansConfiguration.xml` и `Content/*`.
`SecurityConfiguration.xml` не читается — в нём хеши паролей пользователей.

Подробный разбор формата, правило вычисления адреса и архитектура пакета — в
[CLAUDE.md](CLAUDE.md).

## Проверка целостности

`validate_config` находит дефекты, которые в самом Global Monitor не видны:

* **устаревшие подписи на планах** — подпись объекта кэшируется при отрисовке и
  после перенумерации АЛС показывает чужой адрес (в рабочих конфигурациях таких
  находилось до 215 на файл);
* **расхождения в названиях типов** между справочником `drivers.json` и приложением;
* висячие объекты планов, битые ссылки на подложки и сироты в `Content/`.
