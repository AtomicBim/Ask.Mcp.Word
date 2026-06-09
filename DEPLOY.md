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
mkdir -p word_files logs
chown -R "$(id -u):$(id -g)" word_files logs

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

```nginx
# MCP transport (streamable-HTTP)
location /mcp {
    proxy_pass http://word-mcp-server:8018;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

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
