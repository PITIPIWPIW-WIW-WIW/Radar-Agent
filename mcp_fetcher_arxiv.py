import asyncio
import ast
import json
import os
import re
import time
import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import Agent
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv

from agent_manager import get_model

load_dotenv()
logger = logging.getLogger("mcp_fetcher")

ARXIV_CANDIDATES_LIMIT = 15   # сколько статей запрашиваем у search_papers за один вызов
FINAL_ARTICLES_COUNT = 5
FRESHNESS_WINDOW_DAYS = 3     # берём статьи, опубликованные не позже N дней назад

DEFAULT_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL"]


# --- Схема данных: LLM отвечает ТОЛЬКО за тематический отбор id ---

class SelectionResult(BaseModel):
    selected_ids: list[str] = Field(
        description=f"Список arXiv id (до {FINAL_ARTICLES_COUNT}) отобранных статей"
    )


# selector_agent создаётся лениво (не на уровне модуля) — иначе простой импорт
# этого файла (например, для тестов парсинга) требовал бы рабочий MISTRAL_API_KEY.
_selector_agent = None


def _get_selector_agent() -> Agent:
    global _selector_agent
    if _selector_agent is None:
        _selector_agent = Agent(
            model=get_model(),
            output_type=SelectionResult,
            system_prompt=(
                "Ты — технический аналитик, который отбирает статьи для базы знаний по ИИ.\n\n"
                f"Тебе дадут JSON-список кандидатов (статьи с arXiv), у каждого есть поля "
                "id, title, abstract. Список уже отфильтрован по свежести и категориям — "
                f"свежесть можно не учитывать. Отбери до {FINAL_ARTICLES_COUNT} статей, "
                "наиболее релевантных практическим темам AI/ML/DL/NLP (новые модели, методы, "
                "бенчмарки, инструменты). Чисто теоретические математические работы без "
                "прикладной ценности — игнорируй.\n\n"
                "Твоя ЕДИНСТВЕННАЯ задача — вернуть список id выбранных статей. Текст "
                "(abstract) менять, переводить или пересказывать не нужно — он останется "
                "в оригинальном виде без твоего участия.\n\n"
                f"Если подходящих статей меньше {FINAL_ARTICLES_COUNT} — верни столько, "
                "сколько нашлось. Если ни одна не подходит — верни пустой список. Не "
                "выдумывай факты, если данных недостаточно."
            )
        )
    return _selector_agent


ARXIV_SERVER_PARAMS = StdioServerParameters(
    command="uvx",
    args=["arxiv-mcp-server"],
    # Раньше здесь передавался только PATH — на Windows и в некоторых
    # окружениях uvx/venv лезут в HOME/USERPROFILE/APPDATA для кэша и
    # временных файлов, без них подпроцесс может не подняться. Ключи API
    # всё так же не нужны — arxiv-mcp-server работает с публичным arXiv
    # без авторизации, поэтому просто наследуем всё текущее окружение.
    env={**os.environ}
)


# --- Низкоуровневый вызов тула (идентично kaggle-фетчеру) ---

async def _call_tool(session: ClientSession, tool_name: str, arguments: dict) -> str:
    logger.info(f"Вызов MCP-тула '{tool_name}' с аргументами {arguments}")
    response = await session.call_tool(tool_name, arguments=arguments)
    if not response.content:
        return ""
    return "\n".join(
        part.text for part in response.content if hasattr(part, "text") and part.text
    )


def _safe_json_loads(raw: str):
    """Пытается распарсить как JSON, а если не вышло — как Python-литерал
    (сервер иногда отдаёт repr словаря/списка с одинарными кавычками)."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError, TypeError):
        return None


# --- Разбор ответа search_papers ---
# ВАЖНО: это осознанно устойчивая к формату точка входа — у неофициального
# сервера структура ответа не задокументирована жёстко (может быть как
# список, так и словарь-обёртка {"papers": [...]}). Если формат сменится
# и ни один из вариантов не совпадёт, вернётся [] с warning — начинать
# отладку "почему база не пополняется" следует отсюда.

def _extract_papers(raw_search_result) -> list[dict]:
    if isinstance(raw_search_result, list):
        return raw_search_result
    if isinstance(raw_search_result, dict):
        for key in ("papers", "results", "items"):
            value = raw_search_result.get(key)
            if isinstance(value, list):
                return value
    logger.warning(f"Неожиданный формат ответа search_papers: {type(raw_search_result)}")
    return []


def _paper_id(paper: dict) -> str | None:
    return paper.get("id") or paper.get("paper_id") or paper.get("arxiv_id")


# arxiv-mcp-server сам добавляет этот префикс к любому контенту, пришедшему
# с arXiv (пометка "untrusted external content", см. секцию Security в его
# README про prompt injection) — это НЕ часть исходного abstract, а служебная
# метка самого MCP-сервера. Срезаем её, чтобы в БД шёл текст ровно как на
# странице arXiv, без сторонних вставок.
_EXTERNAL_CONTENT_PREFIX_RE = re.compile(r"^\[EXTERNAL CONTENT\]\s*", re.IGNORECASE)


def _paper_abstract(paper: dict) -> str:
    """
    Забираем описание СТРОГО как есть (после срезки служебной метки сервера
    выше), без изменения языка, перевода или пересказа. Разные версии
    сервера могут называть поле по-разному — перебираем варианты названия
    ключа, но само содержимое abstract не трогаем.
    """
    for key in ("abstract", "summary", "description"):
        value = paper.get(key)
        if value and isinstance(value, str) and value.strip():
            cleaned = _EXTERNAL_CONTENT_PREFIX_RE.sub("", value.strip())
            return cleaned.strip()
    return ""


def _paper_url(paper: dict, paper_id: str) -> str:
    url = paper.get("url") or paper.get("abs_url") or paper.get("pdf_url")
    if url:
        return url
    return f"https://arxiv.org/abs/{paper_id}"


# --- Сбор кандидатов ---
# В отличие от kaggle-фетчера, здесь НЕТ второго вызова тула на каждый
# кандидат (там был list + get): search_papers сразу отдаёт abstract,
# а полный текст статьи (download_paper/read_paper) нам не нужен — для
# эмбеддинга и дедупа достаточно описания, а не всей статьи целиком.

async def _collect_candidates(
    session: ClientSession, query: str, categories: list[str], date_from: str
) -> list[dict]:
    raw = await _call_tool(session, "search_papers", {
        "query": query,
        "max_results": ARXIV_CANDIDATES_LIMIT,
        "date_from": date_from,
        "categories": categories,
        "sort_by": "date",
    })
    parsed = _safe_json_loads(raw)
    if parsed is None:
        logger.warning(f"Не удалось распарсить ответ search_papers, сырой ответ: {raw[:500]}")
        return []

    papers = _extract_papers(parsed)
    candidates = []
    for paper in papers:
        paper_id = _paper_id(paper)
        if not paper_id:
            logger.warning(f"Не удалось извлечь id статьи из объекта: {paper}")
            continue

        abstract = _paper_abstract(paper)
        if not abstract:
            logger.warning(f"Пустой abstract для статьи {paper_id}, пропущена.")
            continue

        title = (paper.get("title") or "").strip() or paper_id

        candidates.append({
            "id": paper_id,
            "title": title,
            "abstract": abstract,
            "url": _paper_url(paper, paper_id),
        })
    return candidates


# --- Публичный интерфейс модуля (асинхронный) ---

async def fetch_articles_via_mcp(
    query: str = "large language models",
    categories: list[str] | None = None,
    freshness_days: int = FRESHNESS_WINDOW_DAYS,
) -> list[dict]:
    """
    Возвращает статьи с ОРИГИНАЛЬНЫМ (не переведённым, не пересказанным)
    abstract. LLM отвечает только за тематический отбор среди уже
    отфильтрованных по свежести и категориям кандидатов — сам текст
    abstract она получает лишь для того, чтобы решить, подходит ли статья
    по теме; в возвращаемый payload идёт оригинальный текст из кандидата,
    а не то, что вернула LLM.
    """
    categories = categories or DEFAULT_CATEGORIES
    date_from = (datetime.now(timezone.utc) - timedelta(days=freshness_days)).strftime("%Y-%m-%d")

    articles_payload = []

    logger.info("Запуск подпроцесса arXiv MCP сервера...")
    try:
        async with stdio_client(ARXIV_SERVER_PARAMS) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                candidates = await _collect_candidates(session, query, categories, date_from)

                if not candidates:
                    logger.info("Нет кандидатов для анализа. Завершение работы модуля.")
                    return articles_payload

                candidates_by_id = {c["id"]: c for c in candidates}

                llm_input = [
                    {"id": c["id"], "title": c["title"], "abstract": c["abstract"]}
                    for c in candidates
                ]

                logger.info(f"Отправка {len(llm_input)} кандидатов в Mistral API на тематический отбор...")
                start_time = time.perf_counter()

                try:
                    prompt = (
                        "Вот список кандидатов в формате JSON. Отбери подходящие по теме:\n\n"
                        f"{json.dumps(llm_input, ensure_ascii=False)}"
                    )
                    result = await _get_selector_agent().run(prompt)

                    # dict.fromkeys сохраняет порядок и убирает возможные дубликаты id.
                    # Дополнительно жёстко режем по FINAL_ARTICLES_COUNT в коде —
                    # системный промпт просит модель вернуть "до 5", но это не
                    # контракт, а пожелание: если LLM его проигнорирует и вернёт
                    # больше, лимит всё равно должен соблюдаться на нашей стороне.
                    selected_ids = list(dict.fromkeys(result.output.selected_ids))[:FINAL_ARTICLES_COUNT]

                    for sid in selected_ids:
                        original = candidates_by_id.get(sid)
                        if not original:
                            logger.warning(f"LLM вернула неизвестный id, пропущено: {sid}")
                            continue

                        articles_payload.append({
                            "title": f"[arXiv] {original['title']}",
                            "text": original["abstract"],
                            "source_url": original["url"],
                        })
                except ValidationError as val_err:
                    logger.error(f"Mistral нарушил контракт Pydantic-схемы: {val_err}")
                except Exception as llm_err:
                    logger.error(f"Ошибка при вызове Mistral API: {llm_err}")
                finally:
                    logger.info("Mistral Finished in %.2fs", time.perf_counter() - start_time)

    except Exception as mcp_err:
        logger.error(f"Критический сбой транспорта arXiv MCP: {mcp_err}")
        raise

    return articles_payload


# --- Синхронная обёртка под контракт stream_all_new_articles() ---

def stream_arxiv_articles(query: str = "large language models"):
    """
    Синхронный генератор-адаптер над fetch_articles_via_mcp() — тот же
    паттерн, что и stream_kaggle_articles() в mcp_fetcher_kaggle.py:
    внутри всё равно не стримится (сначала собираются все кандидаты, потом
    одним вызовом уходят в LLM), поэтому оборачивание в generator ничего
    не теряет по сравнению с текущим поведением.

    ВАЖНО: asyncio.run() упадёт с RuntimeError, если вызвать эту функцию
    из кода, где уже крутится свой event loop (например, из другого async
    обработчика в пайплайне). Если stream_all_new_articles() когда-нибудь
    станет асинхронным или будет вызываться через asyncio.gather — нужно
    звать fetch_articles_via_mcp() напрямую с await, а не через эту обёртку.
    """
    articles = asyncio.run(fetch_articles_via_mcp(query))
    for article in articles:
        yield article


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=== ЗАПУСК ТЕСТА: MISTRAL + ARXIV MCP ===")
    try:
        res = asyncio.run(fetch_articles_via_mcp("large language models"))
        print(f"\nМодуль отработал успешно. Получено объектов для БД: {len(res)}")
        print(json.dumps(res, indent=2, ensure_ascii=False))
    except Exception as main_e:
        print(f"\nТест завершился критической ошибкой: {main_e}")