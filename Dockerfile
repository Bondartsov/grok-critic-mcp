# FILE: Dockerfile
# VERSION: 1.10.0
# Образ MCP-сервера grok-critic. Конфигурация — только через переменные
# окружения POLZA_* (env_file внутри контейнера не используется).
#
# Сборка:  docker build -t grok-critic .
# Запуск:
#   docker run -i --rm \
#     -e POLZA_API_KEY=pza_... \
#     -e POLZA_PRICE_INPUT_PER_1M=2.6 -e POLZA_PRICE_OUTPUT_PER_1M=6.6 \
#     grok-critic
# Регистрация в MCP-клиенте — transport stdio через docker run -i.

FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# self_update требует git-клона — в контейнере осмысленно только пересоздание образа.
ENV POLZA_ALLOW_SELF_UPDATE=false

RUN useradd --create-home critic
USER critic

ENTRYPOINT ["python", "-m", "grok_critic.server"]
