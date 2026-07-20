# Radar Agent

Автоматизированная система мониторинга новостей и трендов в области ML/AI. Пайплайн опрашивает Hugging Face, arXiv, GitHub, Kaggle и лидерборд LMArena, отсеивает дубликаты через векторное сравнение эмбеддингов, прогоняет уникальный контент через LLM-агента (Mistral, PydanticAI) для генерации summary и тегов, и отдаёт результат через FastAPI-бэкенд в Streamlit-дашборд.

## Архитектурный пайплайн

Точка входа полного цикла — `main.py`. Порядок работы:

1. **Инициализация БД** (`database.init_db`) — создаёт таблицы SQLite, если их ещё нет, и накатывает миграции недостающих колонок.
2. **Обновление витрины трендов HF** (`mcp_fetcher_hf_trending.fetch_trending_models`) — отдельный независимый шаг без дедупа и LLM-анализа статей, полностью перезаписывает таблицу `hf_trending`.
3. **Обновление лидерборда Arena** (`arena_scraper.collect_all_categories` → `save_to_db`) — Playwright-скрапинг таблиц по категориям, сохранение снимка и запуск инкрементального LLM-анализа (`leaderboard_analyzer.run_leaderboard_analysis`), который сравнивает текущий снимок с предыдущим и с собственным прошлым текстовым анализом.
4. **Очистка устаревших векторов** (`delete_old_vectors`, старше 14 дней) — сами статьи из базы не удаляются, чистится только кэш векторов для дедупа.
5. **Загрузка векторной памяти** — все векторы из таблицы `vectors` подгружаются в оперативную память одним списком.
6. **Потоковый сбор статей** (`stream_all_new_articles`) — генератор последовательно опрашивает фетчеры HF-моделей, HF-датасетов, arXiv, GitHub и Kaggle; сбой одного источника не останавливает остальные.
7. **Дедупликация** (`dedup.text_to_vector` + `dedup.is_duplicate`) — текст статьи превращается в эмбеддинг (fastembed, модель `paraphrase-multilingual-MiniLM-L12-v2`, 384-мерный вектор, с диск-кэшем по sha256 текста), затем сравнивается по косинусному сходству со всей накопленной памятью векторов. Порог задаётся `DUPLICATE_THRESHOLD` (по умолчанию 0.59, откалиброван в `calibrate_threshold.py`).
8. **Анализ агентом** (`agent_manager.analyze_article`) — уникальный текст уходит в PydanticAI-агента на Mistral, который возвращает структурированные summary и теги; ошибки перехватываются, статья пропускается без остановки цикла.
9. **Сохранение** — вектор и статья пишутся в независимые таблицы (`save_vector`, `save_article`), новый вектор сразу добавляется в память для сравнения со следующими статьями в этом же прогоне.

Отдельно от `main.py` работает `app.py` — FastAPI-сервер, который отдаёт накопленные данные фронтенду (`frontend.py`, Streamlit) через REST API и умеет по запросу запускать скрапинг лидерборда или обновление трендов в фоне (`BackgroundTasks`). `scheduler.py` запускает `main.py` отдельным подпроцессом раз в 3 дня для регулярного автономного обновления данных.

## Состав команды и зоны ответственности

| Участник | Вклад в кодовую базу |
|---|---|
| Климанов Мирослав | База данных SQLite: `database.py` — схема таблиц (`articles`, `vectors`, `leaderboard`, `leaderboard_analysis`, `hf_trending`), миграции колонок, CRUD-функции, ротация снимков лидерборда. |
| Кузьмин Артём Евгеньевич | Бэкенд и фронтенд: `app.py` (FastAPI, эндпоинты `/leaderboard`, `/articles`, `/trending/*`, `/scrape`), `frontend.py` (Streamlit-дашборд), `Dockerfile` и `docker-compose.yml` (контейнеризация api/frontend/worker). |
| Пелько Артём Андреевич | `agent_manager.py` (LLM-агент на PydanticAI/Mistral: анализ статей, лидерборда, саммари трендовых моделей), доработка фетчеров `FetcherHF.py` и `github_fetcher.py`. |
| Новиков Кирилл Витальевич | Тесты дедупликации `tests/test_dedup.py`, фетчер Kaggle `mcp_fetcher_kaggle.py` (MCP-сервер `kaggle-mcp-server`, отбор датасетов и моделей). |
| Сыч Никита Александрович | Парсер лидерборда `arena_scraper.py` (Playwright-скрапинг LMArena по категориям), фетчер `FetcherHFDatasets.py` (датасеты Hugging Face). |
| Дегтярев Владислав Кириллович | Правки и доработки кодовой базы. Определение фреймворка, помощь в разработке тз для модулей, архитектуре |

## Руководство по запуску

### Локальный запуск

```bash
git clone https://github.com/PITIPIWPIW-WIW-WIW/Radar-Agent.git
```

```bash
cd Radar-Agent
```

```bash
python -m venv venv
```

```bash
source venv/bin/activate
```

```bash
pip install -r requirements.txt
```

```bash
playwright install --with-deps chrome
```

```bash
cp .env.example .env
```

Заполнить `.env` реальными ключами (`MISTRAL_API_KEY` обязателен для работы агента; `KAGGLE_USERNAME`/`KAGGLE_KEY`, `HUGGINGFACE_API_KEY`, `GITHUB_TOKEN`, `HF_TRENDING_MCP_PATH` — по источникам).

Запуск полного цикла пайплайна (сбор статей, трендов, лидерборда):

```bash
python main.py
```

Запуск планировщика (повторяет `main.py` раз в 3 дня):

```bash
python scheduler.py
```

Запуск бэкенда (FastAPI):

```bash
uvicorn app:app --reload
```

Запуск фронтенда (Streamlit, в отдельном терминале):

```bash
python -m streamlit run frontend.py
```

Тесты:

```bash
python -m pytest tests/test_dedup.py -v
```

```bash
python smoke_test.py
```

### Запуск через Docker Compose

```bash
docker compose up --build
```

Поднимет три сервиса: `api` (FastAPI, порт 8000), `frontend` (Streamlit, порт 8501), `worker` (`scheduler.py`, регулярный прогон пайплайна). БД и логи хранятся в общем volume `data`.

## Архитектура и описание файлов

**Ядро пайплайна** — `main.py` (оркестрация полного цикла), `config.py` (загрузка настроек из `.env`: ключи API, порог дедупа, пути к логам и БД), `dedup.py` (эмбеддинги через fastembed и косинусное сравнение), `agent_manager.py` (LLM-агенты PydanticAI/Mistral для анализа статей, лидерборда и трендовых моделей), `database.py` (весь слой работы с SQLite, файл БД — `app_database.db`).

**Фетчеры источников** — `FetcherHF.py` и `FetcherHFDatasets.py` (модели и датасеты Hugging Face через MCP), `mcp_fetcher_arxiv.py` (статьи arXiv через MCP), `mcp_fetcher_kaggle.py` (датасеты и модели Kaggle через `kaggle-mcp-server`), `github_fetcher.py` (репозитории GitHub), `mcp_fetcher_hf_trending.py` (витрина трендовых моделей HF через локальный MCP-сервер `hf-trending-mcp`). Каждый фетчер — независимый генератор статей, сбой одного не блокирует остальные.

**Лидерборд** — `arena_scraper.py` (Playwright-скрапинг таблиц LMArena по категориям, сохранение снимков), `leaderboard_analyzer.py` (инкрементальный LLM-анализ снимков с учётом истории).

**Веб-слой** — `app.py` (FastAPI-бэкенд, REST API поверх БД), `frontend.py` (Streamlit-дашборд, обращается к бэкенду по HTTP).

**Автоматизация и обслуживание** — `scheduler.py` (периодический запуск `main.py` отдельным процессом), `calibrate_threshold.py` (подбор порога `DUPLICATE_THRESHOLD` на размеченных парах текстов), `smoke_test.py` (сквозная проверка импортов и основных сценариев без реальных API-ключей), `tests/test_dedup.py` (unit-тесты косинусного сходства и дедупликации).

**Инфраструктура** — `Dockerfile` (образ на `python:3.11-slim` с Playwright/Chrome и `uv` для MCP-серверов), `docker-compose.yml` (сервисы `api`/`frontend`/`worker`), `requirements.txt`, `.env.example` (шаблон переменных окружения).
