import asyncio
import ast
import json
import os
import re
import time
import logging
from datetime import datetime, timezone
from langdetect import detect_langs, LangDetectException, DetectorFactory
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import Agent
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv

from agent_manager import get_model

load_dotenv()
logger = logging.getLogger("mcp_fetcher")

# Фиксируем seed — без этого detect_langs недетерминирован между вызовами
# на одном и том же тексте (задокументированное поведение библиотеки)
DetectorFactory.seed = 0

KAGGLE_BASE_URL = "https://www.kaggle.com"

CANDIDATES_PER_TYPE = 8
FINAL_MATERIALS_COUNT = 5
FRESHNESS_TOP_N = 10  # сколько самых свежих кандидатов (по реальной дате) отдаём на тематический отбор LLM

LANG_DETECTION_CONFIDENCE_THRESHOLD = 0.85
MIN_PROSE_LENGTH_FOR_DETECTION = 40


# --- Схема данных: LLM отвечает ТОЛЬКО за тематический отбор id ---

class SelectionResult(BaseModel):
    selected_ids: list[str] = Field(
        description=f"Список id (до {FINAL_MATERIALS_COUNT}) отобранных материалов"
    )


# selector_agent создаётся лениво (не на уровне модуля) — иначе простой
# импорт kaggle_fetcher.py (например, для тестов _parse_markdown_list или
# _extract_ref_parts, которым Mistral вообще не нужен) требовал бы рабочий
# MISTRAL_API_KEY. Ключ нужен только когда реально доходим до тематического
# отбора через LLM.
_selector_agent = None


def _get_selector_agent() -> Agent:
    global _selector_agent
    if _selector_agent is None:
        _selector_agent = Agent(
            model=get_model(),
            output_type=SelectionResult,
            system_prompt=(
                "Ты — технический аналитик, который отбирает материалы для базы знаний по ИИ.\n\n"
                f"Тебе дадут JSON-список кандидатов (датасеты и модели с Kaggle), у каждого "
                "есть поля id, type, title, full_text. Список уже предварительно отсортирован "
                "по свежести — все кандидаты достаточно новые, свежесть можно не учитывать. "
                f"Отбери до {FINAL_MATERIALS_COUNT} материалов, наиболее релевантных строго "
                "тематикам AI, ML, DL и DS (искусственный интеллект, машинное обучение, "
                "глубокое обучение, наука о данных). Материалы вне этих тематик игнорируй.\n\n"
                "Твоя ЕДИНСТВЕННАЯ задача — вернуть список id выбранных материалов. "
                "Текст менять, переводить или пересказывать не нужно — он останется "
                "в оригинальном виде без твоего участия.\n\n"
                f"Если подходящих материалов меньше {FINAL_MATERIALS_COUNT} — верни столько, "
                "сколько нашлось. Если ни один материал не подходит — верни пустой список. "
                "Не выдумывай факты, если данных недостаточно."
            )
        )
    return _selector_agent


KAGGLE_SERVER_PARAMS = StdioServerParameters(
    command="uvx",
    args=["kaggle-mcp-server"],
    env={
        "KAGGLE_USERNAME": os.getenv("KAGGLE_USERNAME", ""),
        "KAGGLE_KEY": os.getenv("KAGGLE_KEY", ""),
        "PATH": os.getenv("PATH", "")
    }
)


# --- Низкоуровневый вызов тула ---

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
    (сервер иногда отдаёт repr словаря с одинарными кавычками вместо JSON)."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass

    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError, TypeError):
        return None


# --- Парсинг markdown-списков вида "- **Title** (`owner/slug`)" ---
# ВАЖНО: это осознанно хрупкая точка входа — datasets_list/models_list отдают
# Markdown, а не JSON. Если kaggle-mcp-server сменит формат вывода, эта функция
# молча вернёт [] (см. warning в _collect_*_candidates) без явного краша —
# при отладке "почему база не пополняется" начинать проверку отсюда.

_LIST_ENTRY_RE = re.compile(r"^-\s+\*\*(?P<title>.+?)\*\*\s+\(`(?P<ref>[^`]+)`\)", re.MULTILINE)

def _parse_markdown_list(raw_text: str) -> list[dict]:
    items = []
    for m in _LIST_ENTRY_RE.finditer(raw_text):
        items.append({"title": m.group("title").strip(), "ref": m.group("ref").strip()})
    return items


def _extract_ref_parts(item: dict) -> tuple[str, str] | None:
    ref = item.get("ref") or item.get("id")
    if ref and "/" in str(ref):
        owner, slug = str(ref).split("/", 1)
        return owner, slug

    owner = item.get("owner") or item.get("ownerSlug") or item.get("owner_slug")
    slug = item.get("slug") or item.get("datasetSlug") or item.get("dataset_slug") or item.get("modelSlug")
    if owner and slug:
        return owner, slug

    logger.warning(f"Не удалось извлечь owner/slug из объекта: {item}")
    return None


def _extract_full_text(detail: dict) -> str:
    for key in ("description", "subtitle", "overview", "summary"):
        value = detail.get(key)
        if value and isinstance(value, str) and value.strip():
            return value.strip()
    logger.warning(f"Не найдено текстовое поле в JSON-ответе, использую весь объект: {list(detail.keys())}")
    return json.dumps(detail, ensure_ascii=False)


def _parse_last_updated(detail: dict) -> datetime:
    """Достаёт реальную дату последнего обновления материала. Если поле отсутствует
    или не парсится — возвращает минимально возможную дату (материал уйдёт в конец
    при сортировке по свежести, а не сломает пайплайн)."""
    raw = detail.get("lastUpdated") or detail.get("updateTime")
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Kaggle обычно отдаёт даты с 'Z', но на случай строки без смещения
            # приводим к UTC вручную — иначе sort() ниже упадёт при сравнении
            # naive datetime с aware datetime.min от других кандидатов
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        logger.warning(f"Не удалось распарсить дату обновления: {raw}")
        return datetime.min.replace(tzinfo=timezone.utc)


# --- Фильтрация по языку (действует ДО отправки в LLM) ---

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")

def _strip_non_prose(text: str) -> str:
    """Убирает код, ссылки и markdown-разметку, чтобы детектор языка работал на чистой прозе."""
    text = _CODE_BLOCK_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" ", text)
    return text.strip()


def _is_confidently_non_english(text: str) -> bool:
    """
    True только если детектор УВЕРЕННО определил язык как не-английский.
    В спорных случаях (мало текста, низкая уверенность) — считаем английским,
    чтобы не терять хорошие материалы из-за ошибки эвристики на markdown/коде.
    """
    prose = _strip_non_prose(text)
    if len(prose) < MIN_PROSE_LENGTH_FOR_DETECTION:
        return False

    try:
        candidates = detect_langs(prose[:3000])
    except LangDetectException:
        return False

    top = candidates[0]
    if top.lang == "en":
        return False

    return top.prob >= LANG_DETECTION_CONFIDENCE_THRESHOLD


def _filter_english_only(candidates: list[dict]) -> list[dict]:
    kept = []
    for c in candidates:
        if _is_confidently_non_english(c["full_text"]):
            logger.info(f"Отсеян неанглоязычный материал: '{c['title']}' ({c['id']})")
            continue
        kept.append(c)
    return kept


# --- Сбор кандидатов (list + get) для датасетов и моделей ---

async def _collect_dataset_candidates(session: ClientSession, query: str, limit: int) -> list[dict]:
    raw_list = await _call_tool(session, "datasets_list", {"search": query, "page": 1})
    items = _parse_markdown_list(raw_list)
    if not items:
        logger.warning(f"Не удалось распарсить список датасетов, сырой ответ: {raw_list[:500]}")
        return []

    candidates = []
    for item in items[:limit]:
        parts = _extract_ref_parts(item)
        if not parts:
            continue
        owner, slug = parts

        raw_get = await _call_tool(session, "dataset_get", {"owner": owner, "dataset_slug": slug})
        detail = _safe_json_loads(raw_get)

        if not isinstance(detail, dict):
            logger.warning(f"Не удалось распарсить dataset_get для {owner}/{slug}, пропущен.")
            continue

        full_text = _extract_full_text(detail)
        if not full_text:
            logger.warning(f"Пустой full_text для датасета {owner}/{slug}, пропущен.")
            continue

        candidates.append({
            "id": f"dataset:{owner}/{slug}",
            "type": "dataset",
            "title": item["title"],
            "full_text": full_text,
            "last_updated": _parse_last_updated(detail),
            "url": f"{KAGGLE_BASE_URL}/datasets/{owner}/{slug}",
        })
    return candidates


async def _collect_model_candidates(session: ClientSession, query: str, limit: int) -> list[dict]:
    raw_list = await _call_tool(session, "models_list", {"search": query, "page_size": limit})
    items = _parse_markdown_list(raw_list)
    if not items:
        logger.warning(f"Не удалось распарсить список моделей, сырой ответ: {raw_list[:500]}")
        return []

    candidates = []
    for item in items[:limit]:
        parts = _extract_ref_parts(item)
        if not parts:
            continue
        owner, slug = parts

        raw_get = await _call_tool(session, "model_get", {"owner": owner, "model_slug": slug})
        detail = _safe_json_loads(raw_get)

        if not isinstance(detail, dict):
            logger.warning(f"Не удалось распарсить model_get для {owner}/{slug}, пропущена.")
            continue

        full_text = _extract_full_text(detail)
        if not full_text:
            logger.warning(f"Пустой full_text для модели {owner}/{slug}, пропущена.")
            continue

        candidates.append({
            "id": f"model:{owner}/{slug}",
            "type": "model",
            "title": item["title"],
            "full_text": full_text,
            "last_updated": _parse_last_updated(detail),
            "url": f"{KAGGLE_BASE_URL}/models/{owner}/{slug}",
        })
    return candidates


# --- Публичный интерфейс модуля (асинхронный) ---

async def fetch_materials_via_mcp(query: str = "machine learning") -> list[dict]:
    """
    Возвращает материалы с ОРИГИНАЛЬНЫМ (не переведённым) текстом. Свежесть определяется
    точной датой из Kaggle API (программно, до LLM); LLM отвечает только за тематический
    отбор среди уже самых свежих кандидатов. Перевод на русский и финальное сжатие —
    отдельный шаг позже, только для материалов, прошедших дедуп по векторам.
    """
    if not os.getenv("KAGGLE_USERNAME") or not os.getenv("KAGGLE_KEY"):
        logger.warning("KAGGLE_USERNAME/KAGGLE_KEY не заданы — запросы к Kaggle, скорее всего, завершатся ошибкой")

    articles_payload = []

    logger.info("Запуск подпроцесса Kaggle MCP сервера...")
    try:
        async with stdio_client(KAGGLE_SERVER_PARAMS) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                dataset_candidates = await _collect_dataset_candidates(session, query, CANDIDATES_PER_TYPE)
                model_candidates = await _collect_model_candidates(session, query, CANDIDATES_PER_TYPE)
                all_candidates = dataset_candidates + model_candidates

                all_candidates = _filter_english_only(all_candidates)

                if not all_candidates:
                    logger.info("Нет кандидатов для анализа. Завершение работы модуля.")
                    return articles_payload

                # --- Сортировка по РЕАЛЬНОЙ дате обновления, до LLM ---
                all_candidates.sort(key=lambda c: c["last_updated"], reverse=True)
                for c in all_candidates[:FRESHNESS_TOP_N]:
                    logger.info(f"Кандидат по свежести: {c['last_updated']} — {c['title']}")

                freshest_candidates = all_candidates[:FRESHNESS_TOP_N]
                candidates_by_id = {c["id"]: c for c in freshest_candidates}

                llm_input = [
                    {"id": c["id"], "type": c["type"], "title": c["title"], "full_text": c["full_text"]}
                    for c in freshest_candidates
                ]

                logger.info(f"Отправка {len(llm_input)} самых свежих кандидатов в Mistral API на тематический отбор...")
                start_time = time.perf_counter()

                try:
                    prompt = (
                        "Вот список кандидатов в формате JSON. Отбери подходящие по теме:\n\n"
                        f"{json.dumps(llm_input, ensure_ascii=False)}"
                    )
                    result = await _get_selector_agent().run(prompt)

                    # dict.fromkeys сохраняет порядок и убирает возможные дубликаты id,
                    # которые LLM ничем не гарантирует не повторить
                    for sid in dict.fromkeys(result.output.selected_ids):
                        original = candidates_by_id.get(sid)
                        if not original:
                            logger.warning(f"LLM вернула неизвестный id, пропущено: {sid}")
                            continue

                        articles_payload.append({
                            "title": f"[Kaggle] {original['title']}",
                            "text": original["full_text"],
                            "source_url": original["url"],
                        })
                except ValidationError as val_err:
                    logger.error(f"Mistral нарушил контракт Pydantic-схемы: {val_err}")
                except Exception as llm_err:
                    logger.error(f"Ошибка при вызове Mistral API: {llm_err}")
                finally:
                    logger.info("Mistral Finished in %.2fs", time.perf_counter() - start_time)

    except Exception as mcp_err:
        logger.error(f"Критический сбой транспорта Kaggle MCP: {mcp_err}")
        raise

    return articles_payload


# --- Синхронная обёртка под контракт stream_all_new_articles() ---

def stream_kaggle_articles(query: str = "machine learning"):
    """
    Синхронный генератор-адаптер над fetch_materials_via_mcp().

    fetch_materials_via_mcp — async по необходимости (MCP-клиент сам
    поднимает подпроцесс и общается с ним через asyncio), но внутри себя
    и так не стримит данные: сначала собирает всех кандидатов, потом одним
    вызовом отправляет их в LLM и возвращает готовый список (максимум
    FINAL_MATERIALS_COUNT штук). Поэтому оборачивание в generator здесь
    ничего не теряет по сравнению с текущим поведением — просто даёт
    синхронному main.py дёргать этот источник так же, как остальные:

        def stream_all_new_articles():
            yield from stream_github_articles()
            yield from stream_arxiv_articles()
            yield from stream_kaggle_articles()
    """
    articles = asyncio.run(fetch_materials_via_mcp(query))
    for article in articles:
        yield article


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=== ЗАПУСК НАДЕЖНОГО ТЕСТА: MISTRAL + KAGGLE MCP ===")
    try:
        res = asyncio.run(fetch_materials_via_mcp("LLM"))
        print(f"\nМодуль отработал успешно. Получено объектов для БД: {len(res)}")
        print(json.dumps(res, indent=2, ensure_ascii=False))
    except Exception as main_e:
        print(f"\nТест завершился критической ошибкой: {main_e}")