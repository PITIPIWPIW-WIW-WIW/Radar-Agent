import asyncio
import json
import os
import sys
import time
import httpx
import logging
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Изолированный импорт для работы с реальным LLM-агентом
try:
    from pydantic_ai import Agent
    from agent_manager import get_model
except ImportError:
    Agent = None
    def get_model(): return None

load_dotenv()
logger = logging.getLogger("github_fetcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# === ПЕРЕКЛЮЧАТЕЛЬ РЕЖИМА ===
IS_TEST_MODE = True


# === 1. СХЕМА ДЛЯ ИИ-ОТБОРЩИКА ===
#
# ВАЖНО: агент отвечает ТОЛЬКО за отбор id, без перевода/пересказа.
# Для дедупликации и векторизации нужен оригинальный английский текст
# описания репозитория как есть — если пропустить его через LLM (даже
# просто для перевода), в текст неизбежно попадут перефразировки модели,
# и сравнение векторов станет менее точным (мы сравниваем разные статьи
# об одном репо, а не то, что каждый раз по-своему написала нейронка).
# Тот же принцип уже применён в kaggle_fetcher.py и mcp_fetcher_arxiv.py.
class SelectionResult(BaseModel):
    selected_ids: list[str] = Field(
        description="Список id (до 5) отобранных репозиториев вида author/repo"
    )


# === 1.1 МОК ДЛЯ ТЕСТИРОВАНИЯ ===
class MockRunResult:
    def __init__(self, output_data: SelectionResult):
        self.output = output_data

class MockAgent:
    async def run(self, prompt: str) -> MockRunResult:
        logger.info("[MOCK] Агент получил промпт. Имитация генерации ответа (1 сек)...")
        await asyncio.sleep(1)

        test_id = "test-author/test-repo"
        try:
            json_str = prompt.split("Кандидаты для анализа:\n")[-1]
            candidates = json.loads(json_str)
            if candidates:
                test_id = candidates[0]["id"]
        except Exception:
            pass

        mock_data = SelectionResult(selected_ids=[test_id])
        return MockRunResult(mock_data)

_github_agent = None

def _get_github_agent():
    global _github_agent
    if IS_TEST_MODE:
        return MockAgent()

    if _github_agent is None:
        _github_agent = Agent(
            model=get_model(),
            output_type=SelectionResult,
            system_prompt=(
                "Ты — Senior Developer. Тебе дают JSON со списком набирающих "
                "популярность репозиториев на GitHub (id, title, full_text).\n"
                "ТВОЯ ЗАДАЧА: отбери до 5 самых перспективных и релевантных "
                "тематике AI/ML проектов. Верни только список их id. Текст "
                "описания менять, переводить или пересказывать не нужно — "
                "он используется как есть, в оригинале."
            )
        )
    return _github_agent


# === 2. ГЛАВНАЯ ЛОГИКА ===
async def fetch_github_trending(days_back: int = 7, limit: int = 15) -> list[dict]:
    """
    Собирает популярные репозитории, созданные за последние N дней.
    """
    articles_payload = []
    
    # Считаем дату отсечки (например, 7 дней назад)
    date_cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime('%Y-%m-%d')
    query = f"created:>{date_cutoff}"
    
    url = "https://api.github.com/search/repositories"
    params = {
        "q": query,
        "sort": "stars",
        "order": "desc",
        "per_page": limit
    }
    
    headers = {
        "Accept": "application/vnd.github.v3+json"
    }
    
    # GitHub ограничивает поиск без токена (10 запросов в минуту). С токеном - 30.
    github_token = os.getenv("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    else:
        logger.warning("GITHUB_TOKEN не задан. Используется анонимный доступ (жесткие лимиты API).")

    # Прокси (если нужен)
    proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("ALL_PROXY")
    if proxy_url:
        proxy_url = proxy_url.strip()
        if not proxy_url.startswith(("http://", "https://")):
            proxy_url = f"http://{proxy_url}"

    logger.info(f"Запрос к GitHub API: поиск трендов с {date_cutoff}...")
    
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False, proxy=proxy_url) as client:
            response = await client.get(url, params=params, headers=headers, follow_redirects=True)
            
            if response.status_code == 403:
                logger.error("Лимит запросов GitHub API исчерпан. Добавьте GITHUB_TOKEN в .env.")
                return articles_payload
                
            response.raise_for_status()
            data = response.json()
            
            items = data.get("items", [])
            if not items:
                logger.info("Ничего не найдено.")
                return articles_payload
                
            all_candidates = []
            for item in items:
                repo_name = item.get("full_name")
                description = item.get("description") or "Нет описания."
                html_url = item.get("html_url")
                stars = item.get("stargazers_count", 0)
                language = item.get("language") or "Не указан"
                
                # Фильтруем пустые/нерелевантные
                if len(description) < 10:
                    continue
                    
                all_candidates.append({
                    "id": repo_name,
                    "title": repo_name,
                    "full_text": f"Language: {language} | Stars: {stars}\nDescription: {description}",
                    "url": html_url
                })
            
            if not all_candidates:
                return articles_payload
                
            # Берем топ-10 кандидатов для агента
            freshest_candidates = all_candidates[:10]
            candidates_by_id = {c["id"]: c for c in freshest_candidates}
            
            llm_input = [
                {"id": c["id"], "title": c["title"], "full_text": c["full_text"]} 
                for c in freshest_candidates
            ]
            
            # Отдаем в LLM
            start_time = time.perf_counter()
            prompt = "Кандидаты для анализа:\n" + json.dumps(llm_input, ensure_ascii=False)
            
            agent = _get_github_agent()
            result = await agent.run(prompt)
            
            # Формируем итоговый список
            # dict.fromkeys сохраняет порядок и убирает возможные дубликаты id
            selected_ids = list(dict.fromkeys(result.output.selected_ids))[:5]

            for sid in selected_ids:
                original = candidates_by_id.get(sid)
                if not original:
                    logger.warning(f"Агент вернул неизвестный id, пропущено: {sid}")
                    continue

                articles_payload.append({
                    "title": f"[GitHub] {original['title']}",
                    # Оригинальный текст как есть, БЕЗ перевода/пересказа —
                    # для дедупликации и векторизации нужен чистый источник,
                    # а не переформулировка от LLM (см. комментарий у SelectionResult).
                    "text": original["full_text"],
                    "source_url": original["url"],
                })
                
            logger.info("Агент завершил работу за %.2fs", time.perf_counter() - start_time)

    except httpx.RequestError as e:
        logger.error(f"Ошибка сети при запросе к GitHub: {e}")
    except Exception as e:
        logger.error(f"Сбой в пайплайне GitHub: {e}")
        
    return articles_payload


# === СИНХРОННАЯ ОБЁРТКА ПОД КОНТРАКТ stream_all_new_articles() ===
def stream_github_articles(days_back: int = 7, limit: int = 15):
    """Синхронный генератор-адаптер над fetch_github_trending()."""
    articles = asyncio.run(fetch_github_trending(days_back, limit))
    for article in articles:
        yield article


# === ТЕСТОВЫЙ ЗАПУСК ===
if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    print("=== ТЕСТОВЫЙ ЗАПУСК GITHUB FETCHER ===")
    res = asyncio.run(fetch_github_trending())
    print(f"\n✅ Получено репозиториев: {len(res)}\n")
    print(json.dumps(res, indent=2, ensure_ascii=False))