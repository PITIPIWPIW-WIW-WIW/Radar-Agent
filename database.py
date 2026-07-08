import sqlite3
import json
from datetime import datetime, timedelta

DB_NAME = 'app_database.db'

from contextlib import contextmanager

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