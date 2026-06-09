# Развёртывание Word MCP Server в Docker

Развёртывание под Open WebUI (движок Claude Opus) во внутренней корпоративной
сети. Доступ снаружи — через общий nginx по адресу
`https://word-mcp.ai.atomsk.ru/mcp`, поэтому порт наружу не публикуется:
внутри docker-сети `ai_network` сервис доступен как
`http://word-mcp-server:8018/mcp`.

## Что в репозитории

- `Dockerfile` — multi-stage, uv-based, runtime на `python:3.12-slim`.
- `docker-compose.yml` — сервис `word-mcp-server`, сеть `ai_network` (external).
- `.env.example` — шаблон переменных окружения для compose.
- `.dockerignore` — отсекает тесты/доки/мусор из build-контекста.

Транспорт — `streamable-http` (`MCP_TRANSPORT=streamable-http` в
`word_document_server/main.py`), путь `/mcp`, порт `8018`.

## Подготовка ВМ

```bash
# 1. Клонировать репозиторий
git clone <repo-url> word-mcp && cd word-mcp

# 2. Подготовить .env (UID/GID хоста для bind-mount'ов)
cp .env.example .env
echo "UID=$(id -u)" >> .env
echo "GID=$(id -g)" >> .env

# 3. Создать каталоги под volume'ы с правильным владельцем
mkdir -p word_files logs public_files
chown -R "$(id -u):$(id -g)" word_files logs public_files

# 4. Убедиться, что сеть ai_network существует (создаёт инфраструктурный compose)
docker network inspect ai_network >/dev/null 2>&1 || docker network create ai_network
```

## Сборка и запуск

```bash
docker compose build
docker compose up -d
docker compose logs -f word-mcp-server   # проверить, что сервер поднялся
```

Проверка healthcheck'а:

```bash
docker inspect --format='{{json .State.Health}}' word-mcp-server | jq
```

## Подключение nginx

На стороне общего nginx (172.18.0.5 в `ai_network`) пробросить
`https://word-mcp.ai.atomsk.ru/mcp` → `http://word-mcp-server:8018/mcp`.
Не забыть про SSE/streamable-HTTP: отключить буферизацию и поднять таймауты.

Кроме `/mcp` тот же контейнер раздаёт **публичные .docx** по
`/files/<uuid>__<name>.docx` (см. раздел `publish_word_file` ниже). В
существующей конфигурации `server_name word-mcp.ai.atomsk.ru` уже
используется `location /` с `proxy_pass http://word-mcp-server:8018`,
поэтому отдельный блок не нужен — оба пути уходят на тот же upstream:

```nginx
server {
    listen 443 ssl;
    server_name word-mcp.ai.atomsk.ru;
    ssl_certificate     /etc/letsencrypt/live/ai.atomsk.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ai.atomsk.ru/privkey.pem;

    client_max_body_size        50M;
    client_header_buffer_size   64k;
    large_client_header_buffers 4 64k;

    location / {
        set $upstream_word_mcp word-mcp-server;
        resolver 127.0.0.11 valid=30s;
        proxy_pass http://$upstream_word_mcp:8018;
        include /etc/nginx/conf.d/include/proxy_params;

        # Streamable HTTP / SSE — потоковая передача, буферизацию отключаем.
        proxy_buffering         off;
        proxy_request_buffering off;
        proxy_cache             off;
    }
}
```

После этого URL `https://word-mcp.ai.atomsk.ru/files/<uuid>__<name>.docx`
(значение `MCP_PUBLIC_BASE_URL=https://word-mcp.ai.atomsk.ru`) становится
кликабельным из Open WebUI.

## Подключение Open WebUI

В Open WebUI добавить MCP-сервер с URL
`https://word-mcp.ai.atomsk.ru/mcp` (транспорт streamable-http).

## Работа с Word-файлами

Внутри контейнера рабочий каталог процесса — `/app/word_files`
(см. `WORKDIR` в `Dockerfile`), он смонтирован на хостовой `./word_files`.
Все инструменты MCP (`create_document`, `copy_document`,
`list_available_documents`, `add_paragraph`, ...) принимают относительные
пути — и сохраняют/читают файлы именно из этого каталога.

Кладите пользовательские `.docx` в `./word_files` на хосте, либо
синхронизируйте каталог с сетевой шарой.

## Возврат файла пользователю (`publish_word_file`)

Чтобы агент в Open WebUI смог дать пользователю прямую ссылку на скачивание
сгенерированного документа, нужно вызвать MCP-инструмент `publish_word_file`:

```jsonc
{ "filename": "report.docx", "download_name": "Q3-report" }
```

Инструмент:

1. Копирует `./word_files/report.docx` в `./public_files/<uuid>__Q3-report.docx`.
2. Возвращает URL вида
   `https://word-mcp.ai.atomsk.ru/files/<uuid>__Q3-report.docx`.
3. Сервер раздаёт эту копию через тот же FastMCP-Starlette app
   (роут регистрируется в `register_http_routes()`).

Через `MCP_FILES_TTL_HOURS=24` фоновый daemon-поток в контейнере
ежечасно удаляет копии старше указанного срока (`0` — отключить очистку).

Если переменная `MCP_PUBLIC_BASE_URL` пустая — инструмент вернёт путь
в файловой системе и предупреждение: качать будет неоткуда, нужно
поднять nginx и заполнить переменную.

## Обновление

```bash
git pull
docker compose build
docker compose up -d
```

## Замечания по безопасности

- Контейнер запускается от непривилегированного пользователя (UID/GID из `.env`).
- Внешний порт не публикуется — доступ только через nginx и/или контейнеры
  в `ai_network`.
- Рабочий каталог процесса — `/app/word_files`, что ограничивает действия
  по умолчанию пределами смонтированного каталога. Тем не менее, текущий
  код **не валидирует** абсолютные пути в аргументах `filename` —
  если планируется принимать запросы от недоверенных пользователей,
  стоит добавить аналог `get_excel_path()` (валидация / запрет path
  traversal) в `word_document_server/utils/`.
