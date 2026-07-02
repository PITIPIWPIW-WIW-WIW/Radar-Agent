import logging

import numpy as np

import config

from dedup import text_to_vector, is_duplicate
from agent_manager import analyze_article, AnalysisError


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


# моки над будет заменить на реальные парсер и БД в будущем, но сигнатура функций останется той же.

def stream_all_new_articles():
    """Мок-парсер: yield-ит по одному словарю статьи вида {title, text, source_url}."""
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


def get_all_vectors() -> list[np.ndarray]:
    """Мок БД: возвращает пустой список векторов (как будто база пуста)."""
    return []


def save_article(article: dict) -> None:
    """Мок БД: вместо записи в SQLite — логирует итоговый payload."""
    logger.info(f"[SAVE] {article['title']} | tags={article['tags']}")


#  Векторизация (заглушка, заменить на реальные эмбеддинги Mistral) 

#  Главный цикл 

def main():
    setup_logging()

    # 1. ОДИН РАЗ выгружаем все векторы из БД в оперативную память
    memory_vectors: list[np.ndarray] = get_all_vectors()
    logger.info(f"Загружено векторов из БД: {len(memory_vectors)}")

    processed, skipped, failed = 0, 0, 0

    # 2. Потоково получаем новые статьи
    for article in stream_all_new_articles():
        title = article["title"]
        text = article["text"]
        source_url = article["source_url"]

        # 3. Текст -> вектор
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

        # 6. Формируем итоговый payload под схему БД
        payload = {
            "title": title,
            "summary": analysis.summary,
            "source_url": source_url,
            "tags": ",".join(analysis.tags),
            "text_vector": new_vector.tolist(),  # для TEXT-поля как JSON-строка
        }

        # 7. Сохраняем в БД
        save_article(payload)
        processed += 1

        # 8. КРИТ ШАГ: пополняем memory_vectors,
        #    чтобы следующие статьи в этом же цикле сравнивались
        #    и с базой, и с только что спарсенными
        memory_vectors.append(new_vector)

    logger.info(f"Готово. Обработано: {processed}, дублей: {skipped}, ошибок: {failed}")


if __name__ == "__main__":
    main()