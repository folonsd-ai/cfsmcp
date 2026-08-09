# Проект: MCP-сервер по структуре метаданных 1С (артефакт)

## Цель
Docker-контейнер с MCP-сервером для поиска по структуре метаданных 1С на основе
выгрузок «Отчёта по структуре метаданных» (txt). Произвольное количество
конфигураций/расширений (в т.ч. несколько версий одной КФ под разными именами),
управление через веб-интерфейс (upload, включение/отключение, удаление;
переиндексация стартует автоматически после parse). Выбор embedding-модели
(LM Studio) и число потоков индексации (1/2/4). Контекст поиска — имя
конфигурации/расширения.

## Зафиксированные решения
| Вопрос | Решение |
|---|---|
| Формат данных | «Отчёт по структуре метаданных», txt; кодировка снайфом (UTF-16LE BOM / UTF-8 / cp1251) |
| Контекст | Имя сущности (`erp` / `Мира` / `МираСклад`), индексы автономные, без мёржа расширений с базой |
| Заимствованные объекты | Хранятся в индексе расширения с маркером `ПринадлежностьОбъекта` + `ОбъектРасширяемойКонфигурации`, фильтр в поиске |
| Попадание сущности | Upload через веб-UI; файл → `METADATA_DIR/e{id}.txt` (не исходное имя — нет коллизий) |
| Имя контекста | Поле UI «Имя контекста» → `name_locked`: MCP-контекст не переименовывается из `Имя` отчёта; ручное **Переименовать** в UI тоже ставит `name_locked` |
| Имя без override | Берётся из `Имя` отчёта (не из имени файла). Если такое имя уже есть — UI спрашивает: слить в существующую или указать другое имя контекста |
| Несколько версий одной КФ | Отказаться от слияния и задать разные имена (`ERP_1.16` / `ERP_1.17`) → отдельные MCP-контексты и индексы |
| Копия сущности | Copy только для `ready`: новое имя → полная копия (файл + SQLite objects/links + zvec), имя `name_locked` |
| Модули BSL | Каталог выгрузки или zip; на сущность: глубина `signatures` / `code` / `full` и эмбеддинг `meta` / `body` / `chunks` (выбор при первой загрузке; при `signatures` эмбеддинг всегда `meta`; дальше без вопроса); на сервер — `*.bsl` и Form.bin; хранение → `…Методы.{role}.{Имя}`; полная очистка — «Удалить BSL»; при parse осиротевшие методы снимаются автоматически |
| BSL в MCP | Колонка BSL: счётчик + `bsl_enabled` (скрытие из search/MCP без удаления); play/pause в строке и в контекстном меню |
| MCP по коду | `list_code_modules` → `list_methods` / `find_methods` → `get_method` (сигнатура + doc-comment, без тела); методы также в общем search при `bsl_enabled` |
| Отключение | Soft-disable (`enabled=0`) скрывает контекст из MCP; выключение контекста → `bsl_enabled=0`; включение → `bsl_enabled=1`, если методы уже есть; флаги в SQLite (`./data`) переживают рестарт |
| Признаки | `tags` (имя + цвет) + `entity_tags` (M2M); настройки по клику на «Признаки»; в строке — chip + × снять / клик — мультивыбор; отбор OR (любой из выбранных); `POST /api/tags/filtered/enable` (`match_all=false` по умолчанию, пустой `tag_ids` = все) — пакетный start/stop (при stop также гасится BSL) |
| UI язык | ru (default) / en; `localStorage` `cfsmcp-lang`; словарь `app/static/i18n.js` (в т.ч. свой file picker — системный `<input type=file>` не локализуется) |
| UI тема | Светлая / тёмная; `localStorage` `cfsmcp-theme` |
| Upload UI | Одна компактная строка (файл / модель / имя / Загрузить); автообновление списка (~1.5 с), отдельной кнопки «Обновить список» нет |
| LM Studio в UI | Статус online/offline — точка на кнопке «Настройки LM Studio» (детали в title) |
| Переиндексация | Автоматически после успешного parse (upload → parse → reindex); Reindex в контекстном меню — повторный запуск |
| Эмбеддинги | LM Studio OpenAI-compatible `/v1/embeddings`; default `text-embedding-multilingual-e5-small` (384d, без префиксов), запасной `e5-large-instruct` (1024d, `query:`/`passage:`); режим текста BSL (`meta` / `body` / `chunks`) выбирается при загрузке модулей и хранится на сущности |
| Выбор модели | Дропдаун на upload + default в настройках LM Studio; модель на сущности; смена модели → полный reindex; коллекция zvec на (сущность × модель) |
| Многопоточная индексация | UI «Embedding workers» = 1 / 2 / 4 (`app_settings`); параллельные HTTP к LM Studio, запись в zvec/SQLite — последовательно; env `EMBEDDING_WORKERS`, `EMBEDDING_BATCH_SIZE` (default 128 объектов), `EMBEDDING_MAX_TEXTS_PER_REQUEST` (default 256 текстов на один `/v1/embeddings` — важно при `chunks`) |
| Хранение | SQLite = источник истины, zvec = пересобираемый индекс; Docker volume `./data:/data` (`DB_PATH`, `METADATA_DIR`, `ZVEC_DIR` под `/data`) |
| MCP-клиент | Cursor, Streamable HTTP; URL `http://host:8558/mcp/` (со слэшем; `/mcp` → `/mcp/`) |
| Имя MCP-сервера | `cfsmcp` |
| Аутентификация | Нет |
| Usage stats | In-memory окно ~10 мин (`/api/stats`); сбрасывается при рестарте процесса |
| Повторный upload той же сущности | Upload в строке (или merge по имени): merge по path+content_hash; эмбеддинги/zvec только по delta (не путать с несколькими версиями через разные имена контекста) |
| Лимит upload | Нет жёсткого cap (отчёты до 300+ МБ); streaming multipart → диск, без буфера всего файла в RAM |
| Reindex | Идемпотентность: второй старт при `status=indexing` — no-op / 409 |
| FTS-стеммер | Русский |
| Порт | Один порт `8558` (UI + `/mcp`) |
| Пайплайн | Upload → фон.парсинг → сразу auto-reindex (эмбеддинги + zvec); Reindex — повторно |
| Группы сущностей | Не используем (были прототипом; удалены в пользу признаков) |

## Формат выгрузки (разбор образца, 8.8 МБ, 117 161 строка, 5 192 узла)
- Дерево с табуляцией; узел: `\t*-\s<полный.путь>`, путь самодостаточен (`Справочники.X.Реквизиты.Y`).
- Свойства: `\t*<Ключ>: "<значение>"`; пустые ключи + список строк в кавычках
  (`Тип:`, `Состав:`, `Движения:`, `ВводитсяНаОсновании:`).
- Первый сегмент пути = тип (Документы, Справочники, Перечисления, РегистрыСведений,
  Обработки, Отчёты, ОбщиеМодули/Формы/Команды/Макеты/Картинки, Роли, Подсистемы,
  ПараметрыСеанса, Языки).
- Глубина: реквизиты, табличные части, значения перечислений, ресурсы/измерения
  регистров, формы, макеты, команды, подсистемы с `Состав:`.
- Связи: `Тип:` (ссылки `СправочникСсылка.X`), `Движения`, `ВводитсяНаОсновании`,
  `Состав` подсистем, ссылки на формы.
- В образце: 759 объектов верхнего уровня; 5 152 собственных, 29 заимствованных.
- Кодировка снайфом (UTF-16LE BOM / UTF-8 / cp1251).

## Архитектура
- **SQLite** (внешний каталог, env `DB_PATH`): сущности (id, имя, тип, версия, файл,
  статус вкл/выкл, модель, статус индексации, parse_gen, parse stats
  `+added ~changed -deleted =unchanged`), объекты (путь, вид, имя, синоним,
  комментарий, принадлежность, базовый объект, props_json, `content_hash`,
  `parse_gen`, `embed_done`), связи (from/to/тип связи),
  `pending_zvec_deletes` (doc_id к удалению из индекса).
- **zvec** (внешний каталог, коллекция на сущность×модель): FTS по полю `text`
  (имя+синоним+комментарий+путь, русский стеммер), вектор `embedding` (COSINE, HNSW),
  фильтры по `kind`/`belong`. Гибридный поиск: вектор + FTS + RRF.
- **Инкремент**: повторный parse мержит по `path`+`content_hash` (без wipe);
  `embed_done` сбрасывается только у new/changed; удалённые пути →
  `pending_zvec_deletes`. Reindex эмбеддит только `embed_done=0`, в zvec —
  `upsert`/`delete`. Полный rebuild (temp+replace) — если коллекции нет или
  смена модели. Повторный reindex при `status=indexing` — 409.
- Парсер стримит узлы (`iter_report_nodes`) батчами по 500: lookup path в SQLite
  (`IN (...)`), merge, `commit` после каждого батча — без полного списка узлов и
  без полного path-index в RAM.
- **Модель и потоки**: embedding-модель выбирается в UI (upload / default в LM Studio
  settings); при reindex — `EMBEDDING_WORKERS` параллельных запросов к LM Studio
  батчами по `EMBEDDING_BATCH_SIZE` (качество векторов от batch/workers не зависит);
  insert/upsert в zvec и `embed_done` — одним потоком. Прогресс UI по `index_target`.

### Пайплайн обработки сущности
1. **Upload** — streaming multipart → `METADATA_DIR`, статус `uploaded`;
  существующие objects не стираются (merge на parse).
2. **Parse (фон)** — стрим → merge SQLite по hash, rebuild links, stats
  `+/~/-/=` в UI; статус `parsed`. При ошибке — `parse_error`.
3. **Reindex** — сразу после успешного parse (и по кнопке Reindex): параллельные
  embedding-запросы к выбранной модели, затем serial zvec write; delta или full
  (нет коллекции / смена модели); `indexing` → `ready`.
4. **MCP** — в `list_contexts` / поиске только `enabled` + `ready`.

## API MCP (FastMCP, streamable HTTP, путь `/mcp`, сервер `cfsmcp`)
- `list_contexts()` — включённые ready-сущности (+ `tags`).
- `list_context_groups()` — признаки с составом контекстов; `context_ref` = `tag:Имя`.
- `search_metadata(context, query, kind?, include_borrowed?)` — FTS; `context` = имя или `tag:…`.
- `semantic_search(context, query, top_n?)` — векторный/гибридный; то же для `tag:…`.
- `get_object(context, path)` / `get_links` / `list_methods` / `get_method` — **только** имя сущности
  (не `tag:`); брать `context` из hit’а поиска.
- Код (BSL): `list_code_modules` (допускает `tag:`) → `list_methods` / `find_methods` → `get_method`.
- Поиск по `tag:` — fan-out по всем ready+enabled с признаком; hit’ы с полем `context`, merge по score.

## Веб-UI (FastAPI)
- `GET /` — страница; `GET /api/entities`, `POST /api/entities/upload`
  (multipart → сразу в `METADATA_DIR`, без загрузки всего тела в память;
  размер не режем заранее), `PATCH /api/entities/{id}` (вкл/выкл + при выкл и BSL,
  `bsl_enabled`, `tag_ids`, модель),
  `DELETE /api/entities/{id}`, `POST /api/entities/{id}/reindex` (ручной повтор;
  идемпотентно при уже идущей индексации; после upload reindex идёт сам),
  Name override для нескольких версий одной КФ; Copy готовой сущности под новым именем;
  Upload в строке → `entity_id` (обновление отчёта / слияние без смены MCP-имени);
  `POST /api/entities/{id}/upload-modules` — zip или multipart `*.bsl` + `Form.bin` → методы;
  `DELETE /api/entities/{id}/modules` — очистка BSL-методов;
  признаки: `GET/POST /api/tags`, `PATCH/DELETE /api/tags/{id}` (имя + цвет),
  `POST /api/tags/filtered/enable` (OR по `tag_ids`, пустой список = все);
  UI — фильтр, цвета, inline-выбор / снятие ×, пакетный start/stop; ru/en + тема;
  выбор embedding-модели на upload; LM Studio — URL, default model, workers 1/2/4,
  индикатор online на кнопке настроек; usage stats.

## Docker
- Образ Python (3.13), зависимости: fastmcp, zvec, fastapi, uvicorn, httpx, python-multipart.
- env: `METADATA_DIR`, `DB_PATH`, `LM_STUDIO_URL` (в docker = `http://host.docker.internal:1234`),
  `EMBEDDING_WORKERS` (default `2`, UI может переопределить 1/2/4), `EMBEDDING_BATCH_SIZE`
  (default `128`), `HOST` (default `0.0.0.0`), `PORT` (default `8558`).
- Один процесс / один порт `8558`: FastAPI + mount FastMCP на `/mcp`
  (`streamable_http_path` / `mount_path`).

## Статус
- zvec 0.6.0: векторный поиск, FTS, RRF, `upsert`/`delete`, `destroy()`.
- Реализовано: стрим-parse + incremental merge/reindex, MCP, веб-UI, Docker (`8558`), usage stats.
