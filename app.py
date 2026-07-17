from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from database import (
    init_db, get_all_leaderboard_snapshots, save_leaderboard_data, get_all_articles,
    get_analysis_history, save_trending_models, get_trending_models,
)
import arena_scraper
from leaderboard_analyzer import run_leaderboard_analysis
from mcp_fetcher_hf_trending import fetch_trending_models

app = FastAPI(title="Arena AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    init_db()


# Внутренняя функция бэкенда, которая соединяет оригинальный скрапер и базу данных
def run_original_scraper_and_save():
    print("[БЭКЕНД] Запуск оригинального утвержденного скрапера...")

    # Передаем словарь URL-адресов точно в таком виде, как в оригинальном main() скрапера
    category_urls = {
        "text": "https://arena.ai/leaderboard/text",
        "vision": "https://arena.ai/leaderboard/vision",
        "search": "https://arena.ai/leaderboard/search",
        "document": "https://arena.ai/leaderboard/document",
        "webdev": "https://arena.ai/leaderboard/code/webdev",
        "image-to-webdev": "https://arena.ai/leaderboard/code/image-to-webdev",
        "text-to-image": "https://arena.ai/leaderboard/text-to-image",
        "image-edit": "https://arena.ai/leaderboard/image-edit",
        "text-to-video": "https://arena.ai/leaderboard/text-to-video",
        "image-to-video": "https://arena.ai/leaderboard/image-to-video",
        "video-edit": "https://arena.ai/leaderboard/video-edit"
    }

    try:
        # 1. Запускаем сбор данных (функция вернет final_snapshot)
        real_data = arena_scraper.collect_all_categories(category_urls)

        # 2. Передаем собранный словарь напрямую в оригинальную функцию сохранения базы данных
        print("[БЭКЕНД] Сбор завершен. Записываем живые данные в базу...")
        save_leaderboard_data(real_data)
        print("[БЭКЕНД] Успешно сохранено! Строим инкрементальный анализ...")

        # Анализ строится СРАЗУ после сохранения нового снимка: агент видит
        # новый снимок, предыдущий снимок и свой же предыдущий анализ.
        run_leaderboard_analysis()
        print("[БЭКЕНД] Анализ лидерборда обновлён!")
    except Exception as e:
        print(f"[БЭКЕНД ОШИБКА] Ошибка при работе связки скрапера и БД: {e}")


# Эндпоинт для вывода лидерборда на фронтенд
@app.get("/leaderboard")
def read_leaderboard():
    try:
        snapshots_list = get_all_leaderboard_snapshots()
        return {
            "status": "success",
            "total_snapshots": len(snapshots_list),
            "data": snapshots_list
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}


# Эндпоинт для фонового запуска оригинального скрапера
@app.post("/scrape")
def trigger_scraping(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_original_scraper_and_save)
    return {
        "status": "success",
        "message": "Оригинальный скрапер запущен! Сбор данных займет около 1-2 минут."
    }


# Эндпоинт для истории анализа лидерборда (от новых к старым)
@app.get("/leaderboard/analysis")
def read_leaderboard_analysis():
    try:
        history = get_analysis_history(limit=10)
        return {
            "status": "success",
            "total_analyses": len(history),
            "data": history,
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}


# Внутренняя функция бэкенда: тянет трендовые модели через hf-trending-mcp и сохраняет
def run_trending_fetch_and_save():
    print("[БЭКЕНД] Запуск сбора трендовых моделей HF (hf-trending-mcp)...")
    try:
        models = fetch_trending_models(limit=15)
        if not models:
            print("[БЭКЕНД] Трендовые модели не получены (пусто или ошибка MCP-сервера).")
            return
        save_trending_models(models)
        print(f"[БЭКЕНД] Сохранено трендовых моделей: {len(models)}")
    except Exception as e:
        print(f"[БЭКЕНД ОШИБКА] Не удалось получить трендовые модели: {e}")


# Эндпоинт для фонового запуска сбора трендовых моделей
@app.post("/trending/refresh")
def trigger_trending_fetch(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_trending_fetch_and_save)
    return {
        "status": "success",
        "message": "Сбор трендовых моделей HF запущен в фоне.",
    }


# Эндпоинт для вывода текущей витрины трендовых моделей
@app.get("/trending/models")
def read_trending_models():
    try:
        models = get_trending_models()
        return {"status": "success", "total_models": len(models), "data": models}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}


# Твой эндпоинт для статей (оставляем без изменений)
@app.get("/articles")
def read_articles():
    articles_list = get_all_articles()
    return {
        "status": "success",
        "total_articles": len(articles_list),
        "data": articles_list
    }