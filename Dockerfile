FROM python:3.11-slim

# curl нужен для установки uv, ca-certificates — для https-запросов
# (Mistral, HF, GitHub, arena.ai и т.д.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uv/uvx — ими запускаются MCP-серверы Kaggle/arXiv/HF (mcp_fetcher_*.py,
# FetcherHF.py, FetcherHFDatasets.py: StdioServerParameters(command="uvx", ...))
RUN pip install --no-cache-dir uv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Реальный Chrome, а не bundled Chromium — arena_scraper.py запускает браузер
# через channel="chrome" именно потому, что голый Chromium банится Cloudflare
# на arena.ai. --with-deps сам подтянет системные библиотеки под apt.
RUN playwright install --with-deps chrome

COPY . .

# Локальный MCP-сервер трендов HF (github.com/kukapay/hf-trending-mcp) —
# предустанавливаем его зависимости на этапе сборки (через uv, требует
# requires-python >=3.13, поэтому обязательно уточни, что образ имеет доступ
# в сеть на сборке — uv сам скачает нужный Python), чтобы не тянуть их
# из сети при каждом первом запуске пайплайна в контейнере.
RUN uv --directory /app/hf-trending-mcp sync

ENV HF_TRENDING_MCP_PATH=/app/hf-trending-mcp
ENV PYTHONUNBUFFERED=1

# Команда по умолчанию — можно переопределить в docker-compose.yml
# (api: uvicorn, frontend: streamlit, worker: scheduler.py)
CMD ["python", "main.py"]
