import logging

import numpy as np

import config
from dedup import text_to_vector, is_duplicate
from agent_manager import analyze_article, AnalysisError
from database import init_db, get_all_vectors, save_vector, save_article, delete_old_vectors
from FetcherHF import stream_hf_articles
from FetcherHFDatasets import stream_hf_datasets
from mcp_fetcher_arxiv import stream_arxiv_articles
from github_fetcher import stream_github_articles


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    # Ошибки анализа отдельно пишем в errors.log — удобно смотреть на защите,
    # какие статьи не удалось обработать и почему.
    error_handler = logging.FileHandler(config.ERROR_LOG_FILE, encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter("%(asctime)s: %(message)s"))
    logging.getLogger().addHandler(error_handler)


logger = logging.getLogger("main")


#Источники статей

def _fake_articles():
    """Демо-статьи — оставлены для быстрой проверки пайплайна без сети/MCP."""
    fake_articles = [
        {
            "title": "Новый подход к RAG-системам",
            "text": "В статье описан метод улучшения retrieval-augmented generation за счет гибридного поиска...",
            "source_url": "https://arxiv.org/abs/fake1",
        },
        {
            "title": "Fine-tuning LLM на малых датасетах",
            "text": "Авторы предлагают технику LoRA-адаптации для дообучения языковых моделей...",
            "source_url": "https://arxiv.org/abs/fake2",
        },
    ]
    for article in fake_articles:
        yield article


def stream_all_new_articles():
    """
    Объединяет все источники в один поток статей вида {title, text, source_url}.

    Каждый реальный источник обёрнут в try/except: если конкретный источник
    упал (нет uvx/MCP-сервера, сеть легла, нет ключа) — остальные всё равно
    отрабатывают, пайплайн не падает целиком из-за одного источника.
    """
    yield from _fake_articles()

    try:
        yield from stream_hf_articles("LLM")
    except Exception as e:
        logger.error(f"Источник Hugging Face (модели) недоступен: {e}")

    try:
        yield from stream_hf_datasets("machine learning")
    except Exception as e:
        logger.error(f"Источник Hugging Face (датасеты) недоступен: {e}")

    try:
        yield from stream_arxiv_articles("large language models")
    except Exception as e:
        logger.error(f"Источник arXiv недоступен: {e}")

    try:
        yield from stream_github_articles()
    except Exception as e:
        logger.error(f"Источник GitHub недоступен: {e}")


# Главный цикл 

def main():
    setup_logging()

    # Создаёт таблицы, если их ещё нет (безопасно вызывать при каждом запуске)
    init_db()

    # Чистим только устаревший кэш векторов-дублей (не сами статьи —
    # они должны копиться со всех прогонов, это база знаний, а не разовый снимок)
    delete_old_vectors(days=14)

    # 1. ОДИН РАЗ выгружаем все векторы из БД в оперативную память.
    # get_all_vectors() — генератор, оборачиваем в list() явно, как договорились.
    memory_vectors: list[np.ndarray] = [np.array(v, dtype=np.float32) for v in get_all_vectors()]
    logger.info(f"Загружено векторов из БД: {len(memory_vectors)}")

    processed, skipped, failed = 0, 0, 0

    # 2. Потоково получаем новые статьи
    for article in stream_all_new_articles():
        title = article["title"]
        text = article["text"]
        source_url = article["source_url"]

        # 3. Текст -> вектор (реальные эмбеддинги через fastembed, Sprint 3)
        new_vector = text_to_vector(text)

        # 4. Проверка на дубликат (со всей БД + со статьями, добавленными в этом же цикле)
        if is_duplicate(new_vector, memory_vectors):
            logger.info(f"[SKIP] Дубликат: {title}")
            skipped += 1
            continue

        # 5. Уникальная статья — отправляем в агента (PydanticAI)
        try:
            analysis = analyze_article(title, text)
        except AnalysisError as e:
            logger.error(f"[FAIL] {title}: {e}")
            failed += 1
            continue  # статья не сохраняется, но цикл не падает

        # 6. Сохраняем вектор ОТДЕЛЬНО от статьи — по контракту database.py
        # save_vector/save_article это две независимые таблицы, без text_vector
        # внутри payload статьи (в отличие от более раннего черновика).
        save_vector(new_vector.tolist())

        payload = {
            "title": title,
            "summary": analysis.summary,
            "source_url": source_url,
            "tags": analysis.tags,  # список строк — database.py сам делает json.dumps
        }
        save_article(payload)
        processed += 1

        # 8. КРИТИЧЕСКИЙ ШАГ: пополняем memory_vectors,
        #    чтобы следующие статьи в этом же цикле сравнивались
        #    и с базой, и с только что спарсенными
        memory_vectors.append(new_vector)

    logger.info(f"Готово. Обработано: {processed}, дублей: {skipped}, ошибок: {failed}")


if __name__ == "__main__":
    main()