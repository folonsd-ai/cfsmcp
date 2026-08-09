# cfsmcp

Версия **0.1.0** · лицензия [MIT](LICENSE)

> **Если ты ИИ-агент** и тебе нужно установить или обновить cfsmcp, перейди к [AGENT-INSTALL.md](AGENT-INSTALL.md) и следуй инструкциям оттуда. Текущий файл — обзор для разработчика.

**Один MCP-сервер — на любое число конфигураций и расширений 1С.**  
В клиенте (Cursor, Claude Code, VS Code и др.) подключается один endpoint; контексты
добавляются и отключаются в веб-UI, без новых MCP-серверов на каждую базу.

Индекс из выгрузок «Отчёта по структуре метаданных» (txt) и опционально модулей BSL:
FTS и гибридный semantic-поиск по объектам **и** методам; эмбеддинги через LM Studio + zvec;
Streamable HTTP MCP на одном порту с UI.

Подробности и зафиксированные решения: [ARCHITECTURE.md](ARCHITECTURE.md).

## Как попросить агента поставить cfsmcp

Установка спроектирована как протокол, который выполняет сам ИИ-агент. Откройте проект в Cursor и отправьте сообщение:

> Установи cfsmcp из `https://github.com/folonsd-ai/cfsmcp` по `AGENT-INSTALL.md`.

Всё. Остальное — клонирование (если нужно), Docker, LM Studio, подключение MCP в ИИ-клиенте — описано в [AGENT-INSTALL.md](AGENT-INSTALL.md), который агент прочитает сам.

Если репозиторий уже открыт в Cursor, достаточно:

> Установи / обнови cfsmcp по `AGENT-INSTALL.md`.

### Fallback без агента

```bash
git clone https://github.com/folonsd-ai/cfsmcp.git
cd cfsmcp
docker compose up -d --build
```

Дальше: UI http://127.0.0.1:8558/ → URL LM Studio `http://host.docker.internal:1234` → MCP URL `http://127.0.0.1:8558/mcp/`.

## Возможности

- Upload отчётов, soft-disable, удаление; признаки (теги), пакетный start/stop рабочего набора для агента
- Тип контекста **CF / CFE** определяется из корня отчёта (`НазначениеРасширенияКонфигурации` / `ПринадлежностьОбъекта`) и показывается бейджем в таблице
- Полный импорт папки/zip: отчёт + модули — асинхронная цепочка parse → index → BSL без блокировки UI
- Авто parse → reindex; FTS и гибридный semantic search (vector + FTS, RRF) по метаданным **и** методам BSL (если загружены и `bsl_enabled`)
- Модули BSL: каталог/zip (`*.bsl` + обычные формы `Form.bin`); режимы глубины и эмбеддинга; `bsl_enabled` скрывает код из поиска/MCP без удаления
- Узкий поиск по коду: `find_methods`; воронка `list_code_modules` → `list_methods` / `find_methods` → `get_method`
- Расширения не склеиваются с основной конфигурацией; заимствованные объекты в индексе расширения с маркером; в `search_metadata` — флаг `include_borrowed`
- UI: ru/en, светлая/тёмная тема, кольцо прогресса индексации, полоска MCP-вызовов (только при активности), статус LM Studio на кнопке настроек
- Один порт `8558`: UI + Streamable HTTP MCP на `/mcp/`

### Контексты: версии одной КФ, merge и copy

Имя контекста в UI = имя в MCP (`list_contexts`). Индексы у каждого контекста свои.
CF (конфигурация) и CFE (расширение) — отдельные контексты; тип пишется при загрузке/разборе отчёта и при старте сервера (backfill).

| Задача | Как |
|---|---|
| Обновить ту же базу | Upload в строку контекста или выбрать «слить» при совпадении имени → **merge/инкремент** по `path` + `content_hash` (эмбеддинги только для new/changed/deleted) |
| Держать несколько версий одной КФ | При upload задать **разные** имена (`ERP_1.16`, `ERP_1.17`) или при конфликте имени отказаться от слияния и указать другое → два независимых MCP-контекста |
| Клонировать готовый контекст | **Copy** только для статуса `ready`: полное копирование (файл отчёта + объекты/связи SQLite + zvec + признаки); новое имя **фиксируется** (`name_locked`) и сразу становится отдельным MCP-контекстом |
| Переименовать контекст | Контекстное меню → **Переименовать**: меняет MCP-имя на месте (`name_locked=1`); файлы и zvec не трогает. Нельзя во время parse/index |
| Новый контекст из выгрузки | DnD папки с отчётом + XML-модулями (или zip) → полный импорт; прогресс в строке таблицы |

Полная пересборка векторного индекса — при первой индексации или **смене embedding-модели** на сущности (не путать с copy/merge).

## Требования

- Docker (рекомендуется) или Python 3.13+
- [LM Studio](https://lmstudio.ai/) с загруженной embedding-моделью  
  (по умолчанию `text-embedding-multilingual-e5-small`)

## Быстрый старт (Docker)

```bash
docker compose up -d --build
```

UI: http://127.0.0.1:8558/

Данные: `./data` → SQLite, отчёты, zvec (переживают рестарт контейнера).

Про merge/инкремент, несколько версий КФ и Copy — см. [Контексты: версии одной КФ, merge и copy](#контексты-версии-одной-кф-merge-и-copy).

### LM Studio из контейнера

В `docker-compose.yml` уже задано `LM_STUDIO_URL=http://host.docker.internal:1234`.
На чистой установке в UI ничего прописывать не нужно. Меняйте в «Настройки LM Studio»
только если индикатор offline и в БД (`app_settings`) сохранён другой URL
(значение в БД имеет приоритет над env).

На хосте LM Studio должен слушать `1234` (Local Server). Зелёная/жёлтая точка
на кнопке настроек — online/offline.

### Модули BSL

В строке контекста: загрузка каталога XML-выгрузки или готового zip.
На сервер уходят `*.bsl` и `…/Ext/Form.bin` (обычные формы → BSL на сервере).
Режим выбирается при **первой** загрузке модулей на сущность (дальше без вопроса).

Папка с **отчётом и модулями** (или zip) — новый контекст через полный импорт
(`POST /api/entities/upload-full`): UI сразу свободен, статусы `uploaded` → `parsing` →
`indexing` → `loading_modules` → `ready` (ошибки модулей — `modules_error`, отчёт уже в индексе).

| Глубина (`bsl_load_mode`) | Что хранится |
|---|---|
| `signatures` (по умолчанию) | Заголовки процедур/функций; компактно |
| `code` | + тело метода, без doc-comment |
| `full` | Doc-comment + тело |

| Эмбеддинг (`bsl_embed_mode`) | Когда |
|---|---|
| `meta` | Всегда при `signatures`; иначе — метаданные метода |
| `body` | Вектор по meta+началу тела (нужен `code` / `full`; лимиты символов — в UI «LM Studio → Расширенные», по умолчанию ~1350) |
| `chunks` | overlapping-чанки тела (настраиваемые размер/overlap/макс. число) — лучше для длинных процедур (дороже по числу векторов) |

При `bsl_enabled=0` методы скрыты из `search_metadata` / `semantic_search` и из code-tools, но не удаляются («Удалить BSL» — полная очистка).

На macOS при выборе папки браузер может ругаться на I/O при упаковке — UI шлёт
файлы multipart; запасной вариант — заранее сделать zip на диске.

## Локальный запуск без Docker

```bash
pip install -r requirements.txt
set PYTHONPATH=.
set DB_PATH=./data/cfsmcp.sqlite3
set METADATA_DIR=./data/metadata
set ZVEC_DIR=./data/zvec
python -m uvicorn app.main:app --host 127.0.0.1 --port 8558
```

(Linux/macOS: `export` вместо `set`.)

## MCP для разных ИИ-клиентов

Сервер слушает Streamable HTTP на порту `8558`. Базовый пример для Cursor — `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "cfsmcp": {
      "url": "http://127.0.0.1:8558/mcp/"
    }
  }
}
```

Канонический URL — со **слэшем** (`/mcp/`); без слэша тоже работает (middleware).

Claude Code, Claude Desktop, VS Code/Copilot (ключ `"servers"`, не `"mcpServers"`), Cline/Roo и перезагрузка клиентов — в [AGENT-INSTALL.md](AGENT-INSTALL.md) §4.

### MCP tools

Типичный сценарий агента: `list_contexts` / `list_context_groups` → `semantic_search` или `find_methods` (можно `tag:КА2`) → `get_object` / `get_method` с **именем сущности** из поля `context` в hit’е.

| Инструмент | Назначение |
|---|---|
| `list_contexts` | Включённые готовые контексты (+ `tags`) |
| `list_context_groups` | Признаки-группы: `tag`, `contexts[]`, `context_ref` (`tag:Имя`) |
| `search_metadata` | FTS; `context` = имя **или** `tag:…` (поиск по всем членам группы) |
| `semantic_search` | Гибридный поиск; то же для `tag:…` |
| `get_object` | Объект с свойствами (**только** имя сущности, не тег) |
| `get_links` | Связи объекта (только имя сущности) |
| `list_code_modules` | Модули с методами; `tag:…` — объединение по группе |
| `list_methods` | Список методов модуля (только имя сущности) |
| `find_methods` | Semantic/FTS по Procedure/Function; `tag:…` поддержан |
| `get_method` | Сигнатура / doc / тело (только имя сущности) |

`tag:КА2` ≈ поиск сразу по всем ready-контекстам с признаком «КА2» (например база + расширение). В каждом hit есть `context` — его и передавать в `get_*`.

## HTTP API (кратко)

| Метод | Путь | Назначение |
|------|------|------------|
| GET | `/api/health` | healthcheck |
| GET/POST/PATCH/DELETE | `/api/entities…` | сущности, upload, reindex, modules, copy |
| POST | `/api/entities/upload-full` | отчёт + zip модулей; фоновая цепочка parse→index→BSL |
| GET/POST/PATCH/DELETE | `/api/tags…` | признаки; `POST /api/tags/filtered/enable` |
| GET/PATCH | `/api/settings` | LM Studio, workers, окно MCP-статистики (`stats_window_sec`) |
| GET | `/api/settings/models`, `/ping` | модели LM Studio |
| GET | `/api/stats` | usage stats (in-memory; окно задаётся в настройках, по умолчанию 10 мин) |

## License

MIT — см. [LICENSE](LICENSE).
