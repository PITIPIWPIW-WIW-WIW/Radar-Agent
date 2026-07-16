"""
Agent Manager — модуль ИИ-агента для анализа статей.
Использует PydanticAI для гарантированного структурированного вывода (summary + tags).
"""

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
    """
    Создаёт свежий MistralModel (со своим http_client) при каждом вызове.
    Ключ проверяется здесь же, а не на уровне модуля — импорт этого файла
    не должен требовать валидный MISTRAL_API_KEY, он нужен только в момент
    реального вызова.
    """
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
    """
    Проверяет, похожа ли ошибка на 429 (rate limit) от Mistral API.
    Free tier Mistral: 2 запроса в секунду — на free tier легко словить 429
    при последовательном парсинге нескольких статей подряд.
    """
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