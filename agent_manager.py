import logging

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.mistral import MistralModel
from pydantic_ai.providers.mistral import MistralProvider
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    before_sleep_log,
)

import config

logger = logging.getLogger("agent_manager")


class ArticleAnalysis(BaseModel):
    """Структурированный результат анализа статьи."""
    summary: str = Field(description="Краткое summary статьи, 2-4 предложения")
    tags: list[str] = Field(description="3-6 релевантных тегов, напр. ['LLM', 'RAG', 'Fine-tuning']")


SYSTEM_PROMPT = """
Ты — эксперт по машинному обучению и NLP. Тебе дают заголовок и текст статьи.
Твоя задача:
1. Написать краткое summary (2-4 предложения).
2. Сгенерировать 3-6 релевантных тегов по теме статьи (технологии, методы, область применения).
Отвечай строго в структурированном виде, без лишнего текста.
"""

def get_model() -> MistralModel:
    if not config.MISTRAL_API_KEY:
        raise RuntimeError(
            "MISTRAL_API_KEY не найден. Создай файл .env на основе .env.example "
            "и укажи там свой ключ."
        )
    http_client = httpx.AsyncClient(timeout=30.0)
    return MistralModel(
        config.MISTRAL_MODEL_NAME,
        provider=MistralProvider(api_key=config.MISTRAL_API_KEY, http_client=http_client),
    )


def _get_article_agent() -> Agent:
    return Agent(
        model=get_model(),
        output_type=ArticleAnalysis,
        system_prompt=SYSTEM_PROMPT,
    )


class AnalysisError(Exception):
    """Кидается, если агент не смог получить валидный структурированный ответ."""


def _is_rate_limit_error(exception: BaseException) -> bool:
    status_code = getattr(exception, "status_code", None)
    if status_code == 429:
        return True
    return "429" in str(exception) or "rate limit" in str(exception).lower()


@retry(
    retry=retry_if_exception(_is_rate_limit_error),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),  # 1s, 2s, 4s, 8s, ... до 30s
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _call_agent(prompt: str):
    """Внутренний вызов агента с retry+backoff только на 429 (rate limit)."""
    return _get_article_agent().run_sync(prompt)


class LeaderboardAnalysis(BaseModel):
    """Структурированный результат инкрементального анализа лидерборда Arena.ai."""
    summary: str = Field(description="Общий обзор текущего состояния лидерборда, 3-5 предложений")
    trends: list[str] = Field(
        description="3-6 ключевых изменений по сравнению с предыдущим анализом: "
                    "новые лидеры, кто вырос/упал в рейтинге, какие тенденции прошлого "
                    "анализа подтвердились или сломались"
    )


LEADERBOARD_SYSTEM_PROMPT = """
Ты — аналитик рынка LLM и генеративных ИИ-моделей. Тебе дают:
1. Текущий снимок лидерборда Arena.ai (топ-5 моделей по категориям с рейтингом Elo).
2. Предыдущий снимок лидерборда (может отсутствовать, если это самый первый анализ).
3. Предыдущий текстовый анализ, который ты сам (или предыдущая версия тебя) написал
   в прошлый раз (может отсутствовать, если это первый анализ).

Твоя задача — построить анализ С УЧЁТОМ ИСТОРИИ, а не пересказать текущие цифры с нуля:
объясни, что изменилось со времени предыдущего анализа (новые лидеры, кто вырос или упал
в рейтинге, какие тенденции из прошлого анализа продолжились, а какие сломались).
Если предыдущего снимка и анализа нет — прямо укажи, что это первый анализ, и опиши
только текущую картину, без сравнений.

Отвечай строго в структурированном виде, без лишнего текста и без markdown-заголовков.
"""


def _get_leaderboard_agent() -> Agent:
    return Agent(
        model=get_model(),
        output_type=LeaderboardAnalysis,
        system_prompt=LEADERBOARD_SYSTEM_PROMPT,
    )


@retry(
    retry=retry_if_exception(_is_rate_limit_error),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _call_leaderboard_agent(prompt: str):
    return _get_leaderboard_agent().run_sync(prompt)


def _format_snapshot(snapshot: dict | None) -> str:
    """Превращает снимок лидерборда в компактный текст для промпта."""
    if not snapshot:
        return "(нет данных — снимок отсутствует)"
    lines = [f"Снимок от {snapshot['fetched_at']}:"]
    for category, models in snapshot["categories"].items():
        models_str = ", ".join(f"{m['name']} ({m['rating']})" for m in models)
        lines.append(f"  - {category}: {models_str}")
    return "\n".join(lines)


def analyze_leaderboard(
    current_snapshot: dict,
    previous_snapshot: dict | None,
    previous_analysis: str | None,
) -> LeaderboardAnalysis:
    """
    Строит НОВЫЙ анализ лидерборда на основе текущего снимка + предыдущего снимка +
    предыдущего текстового анализа. Это инкрементальный анализ: агент видит, что он
    писал в прошлый раз, и продолжает мысль, а не начинает с чистого листа.
    """
    prompt = (
        "=== ТЕКУЩИЙ СНИМОК ===\n"
        f"{_format_snapshot(current_snapshot)}\n\n"
        "=== ПРЕДЫДУЩИЙ СНИМОК ===\n"
        f"{_format_snapshot(previous_snapshot)}\n\n"
        "=== ПРЕДЫДУЩИЙ АНАЛИЗ (текст, написанный тобой ранее) ===\n"
        f"{previous_analysis or '(отсутствует — это первый анализ)'}\n"
    )
    try:
        result = _call_leaderboard_agent(prompt)
        return result.output
    except Exception as e:
        logger.error(f"Ошибка анализа лидерборда: {e}")
        raise AnalysisError(f"Не удалось проанализировать лидерборд: {e}") from e


def analyze_article(title: str, text: str) -> ArticleAnalysis:
    """
    Синхронная обертка над агентом.
    Принимает title+text статьи, возвращает провалидированный ArticleAnalysis.
    При 429 (rate limit) автоматически повторяет запрос с экспоненциальной
    задержкой (до 5 попыток). Любая другая ошибка (невалидный JSON, сеть) —
    сразу оборачивается в AnalysisError, main.py ловит и пропускает статью.
    """
    prompt = f"Заголовок: {title}\n\nТекст статьи:\n{text}"
    try:
        result = _call_agent(prompt)
        return result.output
    except Exception as e:
        logger.error(f"Ошибка анализа статьи '{title}': {e}")
        raise AnalysisError(f"Не удалось проанализировать статью '{title}': {e}") from e