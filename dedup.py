# в этом файле хранятся функции инструменты, в дальнейшем используемые test_dedup.py, это нужно чтобы банально не нагромождать их в main.py
import hashlib
import json
import logging
from pathlib import Path

import numpy as np
import config

logger = logging.getLogger("dedup")

CACHE_DIR = Path(__file__).parent / ".embeddings_cache"
CACHE_FILE = CACHE_DIR / "cache.json"

# Модель грузится лениво (только при первом реальном вызове text_to_vector),
# чтобы просто импортировать dedup.py (например, в тестах на цельность
# косинусного сходства) не тянуло за собой загрузку модели в память.
_model = None

# In-memory кэш эмбеддингов, подгружается с диска при первом обращении.
_cache: dict | None = None


def _get_model():
    """
    Ленивая инициализация модели эмбеддингов.
    Используем fastembed (ONNX Runtime) вместо sentence-transformers —
    та тянет за собой torch, а torch на PyPI по умолчанию тащит ещё и
    CUDA-библиотеки для GPU (гигабайты веса), которые на CPU-only
    машине не нужны и только рвут скачивание на нестабильной сети.

    Модель sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2:
    384-мерные векторы (совпадает с VECTOR_DIM), поддерживает русский
    язык (мультиязычная, ~50 языков), работает полностью локально
    после первого скачивания весов.
    """
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        logger.info("Загружаю модель эмбеддингов paraphrase-multilingual-MiniLM-L12-v2...")
        _model = TextEmbedding(
            model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        )
        logger.info("Модель эмбеддингов загружена.")
    return _model


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f)


def text_to_vector(text: str) -> np.ndarray:
    """
    Реальный эмбеддинг текста через fastembed (замена
    прежней хэш-заглушки, Sprint 3).

    Результат кэшируется на диск по sha256-хэшу текста — если статья
    уже встречалась (тот же текст), эмбеддинг не пересчитывается заново,
    а берётся из кэша. Это экономит время и не грузит модель на CPU
    повторно для одинаковых текстов.
    """
    global _cache
    if _cache is None:
        _cache = _load_cache()

    key = hashlib.sha256(text.encode("utf-8")).hexdigest()

    if key in _cache:
        return np.array(_cache[key], dtype=np.float32)

    model = _get_model()
    # fastembed.embed() принимает список текстов и возвращает генератор
    # numpy-векторов — берём единственный элемент для одного текста.
    vector = next(model.embed([text]))

    _cache[key] = vector.tolist()
    _save_cache(_cache)

    return vector


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def is_duplicate(new_vector: np.ndarray, memory_vectors: list[np.ndarray]) -> bool:
    """Сравнивает new_vector со всеми векторами, накопленными в памяти."""
    for vec in memory_vectors:
        if cosine_similarity(new_vector, vec) >= config.DUPLICATE_THRESHOLD:
            return True
    return False