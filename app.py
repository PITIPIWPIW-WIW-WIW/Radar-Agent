from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
# Импортируем функции из твоей неизмененной базы данных
from database import init_db, get_all_leaderboard_snapshots, save_leaderboard_data, get_all_articles
# Импортируем неизмененный скрапер
import arena_scraper

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
        print("[БЭКЕНД] Успешно сохранено!")
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


# Твой эндпоинт для статей (оставляем без изменений)
@app.get("/articles")
def read_articles():
    articles_list = get_all_articles()
    return {
        "status": "success",
        "total_articles": len(articles_list),
        "data": articles_list
    }