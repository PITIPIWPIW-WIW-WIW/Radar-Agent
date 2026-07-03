import pytest
import numpy as np
import config
import time
from dedup import cosine_similarity, is_duplicate, text_to_vector

#ЕСЛИ НЕ РАБОТАЕТ PYTEST TESTS/TEST_DEDUP.PY, ИСПОЛЬЗОВАТЬ КОМАНДУ python -m pytest tests/test_dedup.py -v ЛИБО python -m pytest -v
def test_identical_vectors():
    """100% дубликат: косинусное сходство идентичных векторов должно быть равно 1.0"""
    vec1 = np.array([1.0, 2.0, 3.0])
    vec2 = np.array([1.0, 2.0, 3.0])
    
    # Используем pytest.approx, так как операции с плавающей точкой 
    # могут выдать 0.9999999999999998 вместо 1.0
    assert cosine_similarity(vec1, vec2) == pytest.approx(1.0)

def test_orthogonal_vectors():
    """Ортогональные векторы (совершенно разные статьи): сходство должно быть 0.0"""
    vec1 = np.array([1.0, 0.0, 0.0])
    vec2 = np.array([0.0, 1.0, 0.0])
    
    assert cosine_similarity(vec1, vec2) == pytest.approx(0.0)

def test_zero_vector():
    """
    Граничный случай: пустой текст дал нулевой вектор.
    Проверяем, что нет ошибки деления на ноль.
    """
    vec_zero = np.array([0.0, 0.0, 0.0])
    vec_normal = np.array([1.0, 1.0, 1.0])
    
    assert cosine_similarity(vec_zero, vec_normal) == 0.0

def test_is_duplicate_boundaries():
    """Пограничные значения порога сходства (по умолчанию 0.92)"""
    # Гарантируем, что порог в конфиге стоит 0.92 для этого теста
    config.DUPLICATE_THRESHOLD = 0.92
    
    base_vector = np.array([1.0, 0.0])
    memory_vectors = [base_vector]
    
    # Вектор со сходством 0.93 (Выше порога -> это дубликат)
    duplicate_similarity = 0.93
    duplicate_vec = np.array([
        duplicate_similarity,
        np.sqrt(1-duplicate_similarity**2)
    ]) 
    assert is_duplicate(duplicate_vec, memory_vectors) is True
    
    # Вектор со сходством 0.91 (Ниже порога -> это УНИКАЛЬНАЯ статья)
    unique_similarity = 0.91
    unique_vec = np.array([
        unique_similarity,
        np.sqrt(1-unique_similarity**2)
    ])
    assert is_duplicate(unique_vec, memory_vectors) is False

#важный момент, следующую функцию нужно запускать с флагом -s для отображения времени выполнения
#медиана времени выполнения по результатам 6 тестов 2,14мс
def test_is_duplicate_benchmark():
    """Замер производительности линейного перебора is_duplicate на ~500 векторах.
    Линейный перебор допустим для MVP — тест фиксирует конкретную цифру
    для аргументации на защите."""
    rng = np.random.default_rng(42)
    memory_vectors = [rng.random(config.VECTOR_DIM) for _ in range(500)]
    query_vector = rng.random(config.VECTOR_DIM)

    start = time.perf_counter()
    is_duplicate(query_vector, memory_vectors)
    elapsed = time.perf_counter() - start

    print(f"\nis_duplicate на 500 векторах (dim={config.VECTOR_DIM}): {elapsed * 1000:.2f} мс")

    # Разумный верхний порог для линейного перебора на MVP — 
    # если тест начнёт падать, это сигнал, что пора переходить на приближённый поиск (ANN)
    assert elapsed < 1.0

def test_memory_vectors_append_logic():
    """
    Проверка отсечения дубликатов внутри одного батча (без БД).
    Имитируем работу главного цикла main.py
    """
    config.DUPLICATE_THRESHOLD = 0.92
    memory_vectors = []
    
    new_article_vector = np.array([1.0, 0.0, 0.0])
    
    # Первая статья уникальна, добавляем её вектор в кэш
    assert is_duplicate(new_article_vector, memory_vectors) is False
    memory_vectors.append(new_article_vector)
    
    # Вторая статья с точно таким же вектором (спарсили то же самое)
    # Алгоритм должен увидеть её в memory_vectors и сказать, что это дубликат
    assert is_duplicate(new_article_vector, memory_vectors) is True

def test_text_to_vector_deterministic():
    """Один и тот же текст должен давать один и тот же вектор
    при разных вызовах (и, что важнее, между разными запусками программы)."""
    v1 = text_to_vector("одна и та же статья про RAG")
    v2 = text_to_vector("одна и та же статья про RAG")
    assert np.array_equal(v1, v2)


def test_empty_text_vector():
    """Пустой текст статьи не должен приводить к падению
    ни при векторизации, ни при последующем сравнении."""
    vec_empty = text_to_vector("")
    memory_vectors = [np.random.default_rng(1).random(config.VECTOR_DIM)]

    # Главное — что это не кидает исключение
    result = is_duplicate(vec_empty, memory_vectors)
    assert isinstance(result, bool)