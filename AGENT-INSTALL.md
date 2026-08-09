# AGENT-INSTALL — установка cfsmcp для ИИ-агента

Этот файл — **протокол установки**. Выполни шаги по порядку, без лишних вопросов, если контекст ясен.

Репозиторий: `https://github.com/folonsd-ai/cfsmcp`

## 0. Где работать

1. Если текущий workspace — уже клон `cfsmcp` (есть `docker-compose.yml`, `app/`, этот файл) — работай **в корне этого репозитория**.
2. Если тебя попросили поставить cfsmcp **в другой проект** (или репозитория ещё нет):
   - сделай shallow-clone в удобное место рядом с проектами пользователя, например:
     ```bash
     git clone --depth 1 https://github.com/folonsd-ai/cfsmcp.git
     ```
   - дальнейшие команды выполняй в корне клона.
3. Не коммить и не пушь без явной просьбы пользователя.

## Цель

1. Поднять cfsmcp в Docker.
2. Проверить UI / health.
3. Подключить MCP-сервер `cfsmcp` в ИИ-клиенте пользователя (Streamable HTTP, URL ниже).
4. Кратко отчитаться.

## 1. Контекст

| Параметр | Значение |
|---|---|
| Порт | `8558` (UI + MCP) |
| UI | http://127.0.0.1:8558/ |
| MCP URL | `http://127.0.0.1:8558/mcp/` (канон со слэшем; без слэша тоже работает) |
| Данные | `./data` (SQLite, отчёты, zvec) |
| Эмбеддинги | LM Studio на хосте, обычно `:1234` |

## 2. Docker

В корне репозитория:

```bash
docker compose up -d --build
```

Первичная проверка (кроссплатформенно, надёжнее для агента на Windows):

```bash
docker compose ps
docker compose logs --tail=50
```

Ожидается сервис `cfsmcp` в состоянии running / healthy, без traceback в логах.  
Если порт `8558` занят — разберись по `ps`/логам, не поднимай второй экземпляр вслепую.

Опционально HTTP health (не полагайся на `curl` в PowerShell: там это алиас `Invoke-WebRequest`, не голый JSON):

```bash
# bash / Git Bash / cmd с реальным curl.exe
curl.exe -s http://127.0.0.1:8558/api/health

# PowerShell
Invoke-RestMethod http://127.0.0.1:8558/api/health
```

## 3. LM Studio

1. На хосте должен быть LM Studio Local Server (`1234`) с embedding-моделью  
   (рекомендуется `text-embedding-multilingual-e5-small`).
2. URL для Docker уже задан в `docker-compose.yml` (`LM_STUDIO_URL=http://host.docker.internal:1234`).  
   На чистой установке **не обязательно** открывать UI и прописывать его снова.  
   Меняй в UI («Настройки LM Studio») только если индикатор **offline** и в `app_settings` ранее сохранён другой URL (значение в БД имеет приоритет над env через `runtime_settings`).
3. Индикатор на кнопке настроек должен стать **online**. Если offline — установку не ломай: явно скажи пользователю проверить LM Studio и при необходимости выставить в UI:

```text
http://host.docker.internal:1234
```

## 4. MCP для разных ИИ-клиентов

Сервер — **Streamable HTTP** на `http://127.0.0.1:8558/mcp/`. Канонический URL — со слэшем; без слэша (`/mcp`) тоже работает — middleware переписывает путь в `/mcp/` (Cursor не следует HTTP-редиректам 307). Подходит любому клиенту с remote MCP по URL, не только Cursor.

### Cursor (по умолчанию для агента)

Создай или дополни `.cursor/mcp.json` в **workspace, где пользователь будет вызывать MCP** (часто — корень этого репозитория или родительский multi-root workspace). Смержи ключ `cfsmcp`, не затирая другие серверы:

```json
{
  "mcpServers": {
    "cfsmcp": {
      "url": "http://127.0.0.1:8558/mcp/"
    }
  }
}
```

В конфиге лучше сразу указывать `/mcp/` (канон).

### Другие клиенты

| Клиент | Файл конфига | Ключ объекта | Нюанс |
|---|---|---|---|
| Cursor | `.cursor/mcp.json` (корень workspace) | `"mcpServers"` | лучше `/mcp/` со слэшем (канон) |
| Claude Code | `.mcp.json` (корень проекта) | `"mcpServers"` | нужен `"type": "http"` (или `streamable-http`) вместе с `"url"` |
| Claude Desktop | `claude_desktop_config.json` — Windows: `%APPDATA%\Claude\`, macOS: `~/Library/Application Support/Claude/` | `"mcpServers"` | `"url"` для удалённых серверов; при необходимости добавь `"type": "http"` |
| VS Code / Copilot | `.vscode/mcp.json` | `"servers"` (**не** `mcpServers`) | частая ошибка — неправильный ключ; для HTTP укажи `"type": "http"` |
| Cline / Roo / прочие | настройка в UI | обычно `"mcpServers"` | URL вводится вручную |

Пример для **Claude Code** (`.mcp.json`):

```json
{
  "mcpServers": {
    "cfsmcp": {
      "type": "http",
      "url": "http://127.0.0.1:8558/mcp/"
    }
  }
}
```

Для **VS Code** тот же сервер, но под ключом `"servers"` (не `"mcpServers"`):

```json
{
  "servers": {
    "cfsmcp": {
      "type": "http",
      "url": "http://127.0.0.1:8558/mcp/"
    }
  }
}
```

После изменения конфига клиент нужно перезапустить / переподключить MCP:

- Cursor — перезагрузка окна;
- Claude Code — перезапуск сессии или `/mcp` → reconnect;
- VS Code — перезагрузка окна;
- Claude Desktop — полный перезапуск приложения.

## 5. Что сообщить пользователю в конце

1. Контейнер: запущен / ошибка.
2. Health: OK / нет.
3. Какой клиент настроен, путь к конфигу и фрагмент с `cfsmcp`.
4. Дальше вручную: убедиться что LM Studio online → загрузить txt-отчёт в UI → дождаться `ready` → `list_contexts`.

## 6. Запреты

- Не удалять `./data`.
- Не менять порт без просьбы.
- Не делать git commit / push без просьбы.
- Не «чинить» offline LM Studio переписыванием половины проекта — достаточно диагностики и инструкции пользователю.

## Обновление

Повторный запуск того же протокола безопасен: `docker compose up -d --build` пересоберёт образ; конфиг MCP обновляй только если URL/сервер ещё не настроены или отличаются.
