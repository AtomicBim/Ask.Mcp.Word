# syntax=docker/dockerfile:1.7
#
# Word MCP Server — production image.
# Multi-stage build:
#   1. builder  — устанавливает зависимости и собирает проект через uv в .venv
#   2. runtime  — минимальный python:slim, копируем готовый /app с .venv
#
# Запуск: word_mcp_server  (FastMCP, транспорт streamable-http,
# слушает $MCP_HOST:$MCP_PORT, путь $MCP_PATH). Пользовательские .docx —
# в /app/word_files (volume), логи — /app/logs.

ARG PYTHON_VERSION=3.12

# ---------- builder ----------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# uv ставим из официального образа — самый быстрый и воспроизводимый путь.
# Тег пинуем явно: воспроизводимые сборки + защита от breaking-изменений lock-формата.
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /usr/local/bin/

WORKDIR /app

# Сначала только метаданные проекта — это даёт хороший cache hit для слоя зависимостей.
COPY pyproject.toml uv.lock README.md ./

# Ставим зависимости из lock-файла (без самого проекта).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Копируем исходники и устанавливаем сам пакет в .venv.
COPY word_document_server ./word_document_server
COPY office_word_mcp_server ./office_word_mcp_server
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    MCP_TRANSPORT=streamable-http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8018 \
    MCP_PATH=/mcp \
    WORD_FILES_PATH=/app/word_files \
    WORD_MCP_LOG_FILE=/app/logs/word-mcp.log

# curl нужен только для healthcheck'а; ставим минимально.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Тащим готовое окружение и исходники из builder'а.
COPY --from=builder /app /app

# Каталоги под bind-mount'ы. Владельца проставит compose (user: UID:GID).
RUN mkdir -p /app/word_files /app/logs

# Все относительные пути в инструментах MCP резолвятся от CWD контейнера,
# поэтому делаем word_files рабочим каталогом — пользовательские .docx
# окажутся в смонтированном на хост каталоге.
WORKDIR /app/word_files

EXPOSE 8018

# Транспорт streamable-http задаётся через MCP_TRANSPORT (см. word_document_server/main.py).
CMD ["word_mcp_server"]
