import sqlite3
import json
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_NAME = 'app_database.db'


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''CREATE TABLE IF NOT EXISTS vectors (
            vector_data TEXT NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );''')
        cursor.execute("""CREATE TABLE IF NOT EXISTS articles (
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            source_url TEXT NOT NULL,
            tags TEXT NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP
        );""")
        # Таблица для лидерборда уже добавлена сюда для порядка
        cursor.execute('''CREATE TABLE IF NOT EXISTS leaderboard (
            category TEXT NOT NULL,
            model_name TEXT NOT NULL,
            rating INTEGER NOT NULL,
            fetched_at TEXT NOT NULL
        );''')
        # Таблица для накопительного (инкрементального) анализа лидерборда.
        # В отличие от leaderboard (там ротация, храним только 2 снимка),
        # тут история НЕ чистится — это база знаний о том, как менялся рынок.
        cursor.execute('''CREATE TABLE IF NOT EXISTS leaderboard_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            analysis_text TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );''')
        conn.commit()


# ==========================================
# ОСНОВНОЕ ХРАНИЛИЩЕ (Векторы и Статьи)
# ==========================================

def save_vector(vector: list[float]) -> None:
    with get_connection() as conn:
        conn.execute('INSERT INTO vectors (vector_data) VALUES (?)', (json.dumps(vector),))


def get_all_vectors():
    with get_connection() as conn:
        cursor = conn.execute('SELECT vector_data FROM vectors')
        for row in cursor:
            yield json.loads(row['vector_data'])


def save_article(article: dict) -> None:
    with get_connection() as conn:
        conn.execute('''
            INSERT INTO articles (title, summary, source_url, tags)
            VALUES (?, ?, ?, ?)
        ''', (article['title'], article['summary'], article['source_url'], json.dumps(article['tags'])))


def get_all_articles() -> list[dict]:
    with get_connection() as conn:
        cursor = conn.execute('SELECT * FROM articles')
        articles = []
        for row in cursor:
            article = dict(row)
            article['tags'] = json.loads(article['tags'])
            articles.append(article)
        return articles


def clear_articles() -> None:
    with get_connection() as conn:
        conn.execute('DELETE FROM articles')


def delete_old_vectors(days: int = 14) -> None:
    cutoff_date = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    with get_connection() as conn:
        conn.execute('DELETE FROM vectors WHERE added_at < ?', (cutoff_date,))


# ==========================================
# ХРАНИЛИЩЕ ЛИДЕРБОРДА (arena.ai)
# ==========================================

def save_leaderboard_data(data: dict) -> None:
    fetched_at = data['fetched_at']
    records_to_insert = []

    # 1. Разворачиваем структуру categories в плоский список записей
    for category, models in data['categories'].items():
        for model in models:
            records_to_insert.append((category, model['name'], model['rating'], fetched_at))

    with get_connection() as conn:
        # 2. Проверяем существование таблицы (на всякий случай, по ТЗ)
        conn.execute('''CREATE TABLE IF NOT EXISTS leaderboard (
            category TEXT NOT NULL,
            model_name TEXT NOT NULL,
            rating INTEGER NOT NULL,
            fetched_at TEXT NOT NULL
        );''')

        # 3. Проверка текущего объема (ищем уникальные даты)
        cursor = conn.execute('SELECT DISTINCT fetched_at FROM leaderboard ORDER BY fetched_at DESC')
        dates = [row['fetched_at'] for row in cursor.fetchall()]

        # 4. Условие удаления: берет самую старую и полностью удаляет
        if len(dates) >= 2:
            oldest_date = dates[-1]
            conn.execute('DELETE FROM leaderboard WHERE fetched_at = ?', (oldest_date,))

        # 5. Массовая вставка всех записей одной операцией
        conn.executemany('''
            INSERT INTO leaderboard (category, model_name, rating, fetched_at)
            VALUES (?, ?, ?, ?)
        ''', records_to_insert)


def save_analysis(fetched_at: str, analysis_text: str) -> None:
    """
    Сохраняет новый текстовый анализ лидерборда. В отличие от save_leaderboard_data,
    НИЧЕГО не удаляет — каждый анализ добавляется к истории, чтобы следующий анализ
    мог опираться на всё, что было написано раньше.
    """
    with get_connection() as conn:
        conn.execute('''
            INSERT INTO leaderboard_analysis (fetched_at, analysis_text)
            VALUES (?, ?)
        ''', (fetched_at, analysis_text))


def get_latest_analysis() -> dict | None:
    """Самый свежий сохранённый анализ (или None, если анализов ещё не было)."""
    with get_connection() as conn:
        cursor = conn.execute(
            'SELECT * FROM leaderboard_analysis ORDER BY id DESC LIMIT 1'
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_analysis_history(limit: int = 10) -> list[dict]:
    """История анализов от новых к старым — для отображения динамики на фронтенде."""
    with get_connection() as conn:
        cursor = conn.execute(
            'SELECT * FROM leaderboard_analysis ORDER BY id DESC LIMIT ?', (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]


def get_all_leaderboard_snapshots() -> list[dict]:
    with get_connection() as conn:
        # Вычитывает все данные из leaderboard
        cursor = conn.execute('SELECT * FROM leaderboard ORDER BY fetched_at DESC')
        rows = cursor.fetchall()

    snapshots_map = {}

    # Код автоматически группирует плоские строки обратно в формат исходных "мега-словарей"
    for row in rows:
        f_at = row['fetched_at']
        if f_at not in snapshots_map:
            snapshots_map[f_at] = {
                "fetched_at": f_at,
                "categories": {}
            }

        cat = row['category']
        if cat not in snapshots_map[f_at]["categories"]:
            snapshots_map[f_at]["categories"][cat] = []

        snapshots_map[f_at]["categories"][cat].append({
            "name": row['model_name'],
            "rating": row['rating']
        })

    return list(snapshots_map.values())


# ==========================================
# БЛОК ДЛЯ ТЕСТИРОВАНИЯ И ПРОСМОТРА ТАБЛИЦ
# ==========================================
if __name__ == '__main__':
    print("--- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ---")
    init_db()
    print("Таблицы успешно созданы.\n")

    print("--- СОХРАНЕНИЕ ТЕСТОВЫХ ДАННЫХ ---")
    # Добавляем статью
    save_article({
        "title": "GPT-5 взломал Пентагон",
        "summary": "Шуточная статья для теста.",
        "source_url": "https://test.com",
        "tags": ["ии", "новости"]
    })

    # Добавляем вектор
    save_vector([0.1, 0.2, 0.3])

    # Добавляем 3 слепка лидерборда (чтобы проверить ротацию).
    # Старый "Слепок 1" должен автоматически удалиться!
    snapshot_1 = {"fetched_at": "2026-07-01", "categories": {"text": [{"name": "Model-A", "rating": 1000}]}}
    snapshot_2 = {"fetched_at": "2026-07-02", "categories": {"text": [{"name": "Model-B", "rating": 1100}]}}
    snapshot_3 = {"fetched_at": "2026-07-03", "categories": {"text": [{"name": "Model-C", "rating": 1200}]}}

    save_leaderboard_data(snapshot_1)
    save_leaderboard_data(snapshot_2)
    save_leaderboard_data(snapshot_3)  # Тут сработает удаление первого слепка
    print("Данные сохранены! Ротация лидерборда отработала.\n")

    print("    ВИЗУАЛИЗАЦИЯ ТАБЛИЦ ИЗ БАЗЫ ДАННЫХ    ")

    # 1. Показываем статьи
    print(">>> ТАБЛИЦА: articles")
    articles = get_all_articles()
    for a in articles:
        print(f"[{a.get('added_at', 'Только что')}] {a['title']} | Теги: {a['tags']} | Ссылка: {a['source_url']}")
    print("-" * 40)

    # 2. Показываем векторы
    print("\n>>> ТАБЛИЦА: vectors")
    with get_connection() as conn:
        for row in conn.execute('SELECT * FROM vectors'):
            print(f"[{row['added_at']}] Вектор: {row['vector_data']}")
    print("-" * 40)

    # 3. Показываем лидерборд
    print("\n>>> ТАБЛИЦА: leaderboard (Максимум 2 слепка!)")
    snapshots = get_all_leaderboard_snapshots()
    for snap in snapshots:
        print(f"Слепок от: {snap['fetched_at']}")
        for cat, models in snap['categories'].items():
            print(f"  Категория '{cat}':")
            for m in models:
                print(f"    - Модель: {m['name']}, Рейтинг: {m['rating']}")