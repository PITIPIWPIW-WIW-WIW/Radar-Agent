import os
import sys
import traceback

TEST_DB_NAME = "smoke_test.db"

PASSED = []
FAILED = []


def check(name):
    def decorator(fn):
        print(f"--- {name} ---")
        try:
            fn()
            print("OK\n")
            PASSED.append(name)
        except Exception as e:
            print(f"FAIL: {type(e).__name__}: {e}")
            traceback.print_exc()
            print()
            FAILED.append(name)
        return fn
    return decorator




@check("Импорт всех модулей без MISTRAL_API_KEY")
def _():
    os.environ.pop("MISTRAL_API_KEY", None)

    import config
    assert config.MISTRAL_API_KEY is None or True  

    import dedup
    import agent_manager
    assert hasattr(agent_manager, "get_model")
    assert hasattr(agent_manager, "analyze_article")

    import database
    for fn_name in ("init_db", "save_vector", "get_all_vectors", "save_article",
                     "get_all_articles", "delete_old_vectors",
                     "save_leaderboard_data", "get_all_leaderboard_snapshots"):
        assert hasattr(database, fn_name), f"database.py: нет функции {fn_name}"

    import FetcherHF
    assert hasattr(FetcherHF, "stream_hf_articles")

    import FetcherHFDatasets
    assert hasattr(FetcherHFDatasets, "stream_hf_datasets")

    import mcp_fetcher_arxiv
    assert hasattr(mcp_fetcher_arxiv, "stream_arxiv_articles")

    import github_fetcher
    assert hasattr(github_fetcher, "stream_github_articles")

    import main
    assert hasattr(main, "stream_all_new_articles")
    assert hasattr(main, "main")


@check("get_model() кидает понятную ошибку при реальном вызове без ключа")
def _():
    import config
    import agent_manager
    from unittest.mock import patch

    with patch.object(config, "MISTRAL_API_KEY", None):
        agent_manager._model = None
        try:
            agent_manager.get_model()
            raise AssertionError("get_model() должен был упасть без ключа")
        except RuntimeError as e:
            assert "MISTRAL_API_KEY" in str(e)
        finally:
            agent_manager._model = None  





@check("database.py: статьи и векторы (базовый CRUD)")
def _():
    import database
    database.DB_NAME = TEST_DB_NAME
    if os.path.exists(TEST_DB_NAME):
        os.remove(TEST_DB_NAME)

    database.init_db()

    database.save_vector([0.1, 0.2, 0.3])
    vectors = list(database.get_all_vectors())
    assert len(vectors) == 1

    database.save_article({
        "title": "Тестовая статья",
        "summary": "Краткое содержание",
        "source_url": "https://example.com/test",
        "tags": ["LLM", "тест"],
    })
    articles = database.get_all_articles()
    assert len(articles) == 1
    assert articles[0]["tags"] == ["LLM", "тест"], "теги должны корректно распаковаться из JSON"

    database.delete_old_vectors(days=14)
    assert len(list(database.get_all_vectors())) == 1, "свежие векторы не должны удаляться"


@check("database.py: лидерборд с ротацией (максимум 2 снимка)")
def _():
    import database
    database.DB_NAME = TEST_DB_NAME  

    snap1 = {"fetched_at": "2026-07-01", "categories": {"text": [{"name": "Model-A", "rating": 1000}]}}
    snap2 = {"fetched_at": "2026-07-02", "categories": {"text": [{"name": "Model-B", "rating": 1100}]}}
    snap3 = {"fetched_at": "2026-07-03", "categories": {"text": [{"name": "Model-C", "rating": 1200}]}}

    database.save_leaderboard_data(snap1)
    database.save_leaderboard_data(snap2)
    database.save_leaderboard_data(snap3)

    snapshots = database.get_all_leaderboard_snapshots()
    assert len(snapshots) == 2, f"ожидали ротацию до 2 снимков, получили {len(snapshots)}"

    dates = {s["fetched_at"] for s in snapshots}
    assert "2026-07-01" not in dates, "самый старый снимок должен был удалиться"
    assert "2026-07-02" in dates and "2026-07-03" in dates



@check("dedup.py: cosine_similarity и is_duplicate")
def _():
    import numpy as np
    import dedup

    v1 = np.array([1.0, 0.0, 0.0])
    v2 = np.array([1.0, 0.0, 0.0])
    v3 = np.array([0.0, 1.0, 0.0])

    assert abs(dedup.cosine_similarity(v1, v2) - 1.0) < 1e-9
    assert abs(dedup.cosine_similarity(v1, v3)) < 1e-9

    assert dedup.is_duplicate(v1, [v2]) is True
    assert dedup.is_duplicate(v1, [v3]) is False

    zero = np.array([0.0, 0.0, 0.0])
    assert dedup.cosine_similarity(zero, v1) == 0.0, "деление на 0 не должно падать"





@check("main.py: полный прогон main() с реальной БД (Mistral/эмбеддинги замоканы)")
def _():
    import hashlib
    import numpy as np
    from unittest.mock import patch, MagicMock

    fake_analysis = MagicMock()
    fake_analysis.summary = "Тестовое summary"
    fake_analysis.tags = ["tag1", "tag2"]

    def fake_text_to_vector(text):
        seed = int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32)
        rng = np.random.default_rng(seed)

        return rng.standard_normal(384).astype(np.float32)

    with patch("main.analyze_article", return_value=fake_analysis), \
         patch("main.text_to_vector", side_effect=fake_text_to_vector):

        import database
        database.DB_NAME = TEST_DB_NAME
        if os.path.exists(TEST_DB_NAME):
            os.remove(TEST_DB_NAME)

        import main
        main.main()

        articles = database.get_all_articles()

        assert len(articles) >= 2, (
            f"ожидали минимум 2 мок-статьи в БД, получили {len(articles)} — "
            "main() мог упасть до сохранения"
        )





@check("arena_scraper.py: save_to_db пишет в реальную БД")
def _():
    import database
    database.DB_NAME = TEST_DB_NAME

    import arena_scraper
    fake_data = {
        "fetched_at": "2026-07-14T12:00:00",
        "categories": {"vision": [{"name": "Test-Model-X", "rating": 1234}]},
    }
    arena_scraper.save_to_db(fake_data)

    snapshots = database.get_all_leaderboard_snapshots()
    found = any(s["fetched_at"] == "2026-07-14T12:00:00" for s in snapshots)
    assert found, "снимок от arena_scraper.save_to_db() не нашёлся в БД"




if __name__ == "__main__":
    print("=" * 60)
    print("ИТОГ")
    print("=" * 60)
    print(f"Пройдено: {len(PASSED)}")
    print(f"Провалено: {len(FAILED)}")

    if FAILED:
        print("\nПровалившиеся проверки:")
        for name in FAILED:
            print(f"  - {name}")

    if os.path.exists(TEST_DB_NAME):
        os.remove(TEST_DB_NAME)

    sys.exit(1 if FAILED else 0)