# в этом файле хранятся функции инструменты, в дальнейшем используемые test_dedup.py, это нужно чтобы банально не нагромождать их в main.py
import numpy as np
import config
import hashlib

def text_to_vector(text: str) -> np.ndarray:
    """
    ЗАГЛУШКА: детерминированный псевдо-вектор на основе хэша текста.
    Используется hashlib (не встроенный hash()) — hash() в Python
    рандомизирован между запусками процесса (PYTHONHASHSEED), а нам
    нужна детерминированность и между перезапусками программы тоже.
    Позже заменить на реальный вызов Mistral Embeddings API (Sprint 3).
    """
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    seed = int(digest, 16) % (2**32)
    rng = np.random.default_rng(seed)
    return rng.random(config.VECTOR_DIM)

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
