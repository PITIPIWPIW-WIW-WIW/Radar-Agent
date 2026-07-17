import asyncio
import json
import os
import re
import sys
import time
import httpx
import logging
from datetime import datetime, timezone

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Попытка импорта реальных зависимостей (оборачиваем в try-except для изоляции)
try:
    from pydantic_ai import Agent
    from agent_manager import get_model
except ImportError:
    Agent = None
    def get_model(): return None

# Настройки логирования
load_dotenv()
logger = logging.getLogger("hf_fetcher")
# ВАЖНО: НЕ вызываем basicConfig() на уровне модуля — main.py импортирует
# этот файл раньше, чем успевает отработать main.setup_logging(), и Python
# молча игнорирует все последующие basicConfig()-вызовы. Раньше это намертво
# запирало LOG_LEVEL из .env на INFO для всего приложения. Настройка для
# прямого запуска — в блоке if __name__ == "__main__" в конце файла.

# === ПЕРЕКЛЮЧАТЕЛЬ РЕЖИМА ===
# Установи False, когда команда закончит работу над LLM-агентом
IS_TEST_MODE = False


# === 1. СХЕМЫ ДЛЯ ИИ-ПЕРЕВОДЧИКА ===
class SelectedMaterial(BaseModel):
    id: str = Field(description="ID выбранной модели (например, hf_model:meta-llama/Llama-2-7b)")
    russian_translation: str = Field(
        description="Подробный, технически грамотный перевод описания модели на русский язык."
    )

class SelectionResult(BaseModel):
    selected_materials: list[SelectedMaterial] = Field(
        description="Список (до 5) отобранных материалов с их переводом"
    )

# === 1.1 МОКИ ДЛЯ ТЕСТИРОВАНИЯ ===
class MockRunResult:
    def __init__(self, output_data: SelectionResult):
        self.output = output_data

class MockAgent:
    """Заглушка, имитирующая поведение pydantic_ai Agent."""
    async def run(self, prompt: str) -> MockRunResult:
        logger.info("[MOCK] Агент получил промпт. Имитация генерации ответа (1 сек)...")
        await asyncio.sleep(1)
        
        test_id = "hf_model:dummy/test-model"
        try:
            json_str = prompt.split("Кандидаты для анализа и перевода:\n")[-1]
            candidates = json.loads(json_str)
            if candidates:
                test_id = candidates[0]["id"]
        except Exception:
            pass

        mock_data = SelectionResult(
            selected_materials=[
                SelectedMaterial(
                    id=test_id,
                    russian_translation="[МОК-ПЕРЕВОД] Это тестовое описание. Модель отлично справляется с генерацией текста, очищена от маркетинговой шелухи и готова к интеграции."
                )
            ]
        )
        return MockRunResult(mock_data)

def _get_hf_agent():
    """Создаёт реального агента или возвращает заглушку в зависимости от режима.

    Реальный агент НЕ кэшируется (см. фикс в agent_manager.get_model()) —
    этот фетчер гоняется через свой отдельный asyncio.run(), и закэшированный
    синглтон остался бы привязан к закрытому event loop при следующем вызове,
    вызывая 'Event loop is closed'."""
    if IS_TEST_MODE:
        return MockAgent()

    return Agent(
        model=get_model(),
        output_type=SelectionResult,
        system_prompt=(
            "Ты — технический аналитик ИИ. Тебе на вход дают JSON со списком новых AI-моделей...\n"
            "ТВОЯ ЗАДАЧА: Отбери до 5 моделей, сделай перевод, верни JSON."
        )
    )


# === 2. НАСТРОЙКИ СЕРВЕРА MCP ===
HF_SERVER_PARAMS = StdioServerParameters(
    command="uvx",
    args=["huggingface-mcp-server"],
    env={
        # ВАЖНО: huggingface-mcp-server читает именно HF_TOKEN, а не
        # HUGGINGFACE_API_KEY — раньше сервер токен вообще не получал и
        # работал анонимно (рейт-лимиты, нестабильная выдача).
        "HF_TOKEN": os.getenv("HUGGINGFACE_API_KEY", ""),
        "HUGGINGFACE_API_KEY": os.getenv("HUGGINGFACE_API_KEY", ""),
        "PATH": os.getenv("PATH", "")
    }
)


# === 3. ФУНКЦИИ ОЧИСТКИ И СБОРА ===
def clean_hf_readme(text: str) -> str:
    """Очищает Markdown-текст карточки модели от кода и мусора."""
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL) # YAML-шапка
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL) # Блоки кода
    text = re.sub(r"`[^`]*`", "", text) # Инлайн-код
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text) # Картинки
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text) # Ссылки
    text = re.sub(r"^[#]+\s+", "", text, flags=re.MULTILINE) # Разметка заголовков
    text = text.replace("**", "").replace("__", "").replace("*", "") # Жирный/курсив
    text = re.sub(r"\n{3,}", "\n\n", text) # Лишние пустые строки
    return text.strip()

async def fetch_readme_directly(model_id: str) -> str:
    """Обходит баг MCP-сервера и скачивает сырой README файл напрямую, вылечивая прокси."""
    clean_id = model_id.strip()
    url = f"https://huggingface.co/{clean_id}/raw/main/README.md"
    
    # Вытаскиваем системный прокси (если есть) и лечим болезнь httpx
    proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("ALL_PROXY")
    
    if proxy_url:
        proxy_url = proxy_url.strip()
        if not proxy_url.startswith(("http://", "https://")):
            proxy_url = f"http://{proxy_url}"
    
    try:
        # trust_env=False + явный proxy пробивают системные блокировки и кривые настройки
        async with httpx.AsyncClient(timeout=15.0, trust_env=False, proxy=proxy_url) as client:
            response = await client.get(url, follow_redirects=True)
            if response.status_code == 200:
                return response.text
            else:
                logger.warning(f"HuggingFace вернул статус {response.status_code} для {clean_id}")
    except httpx.RequestError as e:
        logger.error(f"Ошибка сети при скачивании {clean_id}: {type(e).__name__}: {e}")
        
    return ""

async def _call_tool(session: ClientSession, tool_name: str, arguments: dict) -> str:
    response = await session.call_tool(tool_name, arguments=arguments)
    if not response.content:
        return ""
    return "\n".join(part.text for part in response.content if hasattr(part, "text") and part.text)


# === 4. ГЛАВНАЯ ЛОГИКА ===
def _suppress_benign_proactor_errors(loop, context):
    """
    На Windows при закрытии stdio-пайпа MCP-подпроцесса asyncio иногда кидает
    безвредный шум "Cancelling an overlapped future failed" (WinError 6) —
    уже ПОСЛЕ того, как подпроцесс отработал и данные забраны. К логике/
    данным отношения не имеет — гасим точечно, остальные ошибки пробрасываем.
    """
    if "Cancelling an overlapped future failed" in context.get("message", ""):
        return
    loop.default_exception_handler(context)


async def fetch_hf_materials_via_mcp(query: str = "text-generation") -> list[dict]:
    """Главная функция для импорта. Ищет модели, скачивает тексты, переводит через агента."""
    articles_payload = []
    asyncio.get_running_loop().set_exception_handler(_suppress_benign_proactor_errors)

    if not os.getenv("HUGGINGFACE_API_KEY"):
        logger.warning("HUGGINGFACE_API_KEY не задан — возможны ограничения со стороны API.")

    logger.info("Запуск подпроцесса Hugging Face MCP...")
    try:
        async with stdio_client(HF_SERVER_PARAMS) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                
                # Шаг 1: Поиск
                search_args = {"query": query, "limit": 15}
                raw_list = await _call_tool(session, "search-models", search_args)
                models_data = json.loads(raw_list)
                
                all_candidates = []
                for model in models_data:
                    model_id = model.get("id")
                    if not model_id: continue
                    
                    # Шаг 2: Скачивание
                    raw_readme = await fetch_readme_directly(model_id)
                    if not raw_readme: continue
                        
                    # Шаг 3: Очистка
                    clean_text = clean_hf_readme(raw_readme)
                    if len(clean_text) < 50: continue
                        
                    all_candidates.append({
                        "id": f"hf_model:{model_id}",
                        "title": model_id,
                        "full_text": clean_text,
                        "url": f"https://huggingface.co/{model_id}",
                        # lastModified — ISO8601-строка от HF API. Строковая сортировка
                        # по ней работает корректно (лексикографический порядок ISO8601
                        # совпадает с хронологическим).
                        "last_modified": model.get("lastModified", ""),
                    })
                
                if not all_candidates:
                    logger.info("Кандидаты не найдены или тексты пустые.")
                    return articles_payload
                
                # Сортируем по свежести (недавно обновлённые — вперёд), а уже потом
                # берём первые 10. Раньше порядок был "как отдал search-models" —
                # то есть по релевантности запросу, а не по дате обновления.
                all_candidates.sort(key=lambda c: c["last_modified"], reverse=True)
                freshest_candidates = all_candidates[:10]
                candidates_by_id = {c["id"]: c for c in freshest_candidates}
                
                llm_input = [
                    {"id": c["id"], "title": c["title"], "full_text": c["full_text"][:2500]} 
                    for c in freshest_candidates
                ]
                
                # Шаг 4: Агент
                start_time = time.perf_counter()
                prompt = "Кандидаты для анализа и перевода:\n" + json.dumps(llm_input, ensure_ascii=False)
                
                agent = _get_hf_agent()
                result = await agent.run(prompt)
                
                # Шаг 5: Форматирование результата
                for item in result.output.selected_materials:
                    original = candidates_by_id.get(item.id)
                    if not original: continue
                        
                    articles_payload.append({
                        "title": f"[HuggingFace] {original['title']}",
                        "text": f"Заголовок: {original['title']}\n\nОписание:\n{item.russian_translation}",
                        "source_url": original["url"],
                    })
                    
                logger.info("Агент завершил работу за %.2fs", time.perf_counter() - start_time)

    except Exception as e:
        logger.error(f"Сбой в пайплайне Hugging Face: {e}")
        
    return articles_payload


# === СИНХРОННАЯ ОБЁРТКА ПОД КОНТРАКТ stream_all_new_articles() ===
#
# fetch_hf_materials_via_mcp — async по необходимости (MCP-клиент сам
# поднимает подпроцесс huggingface-mcp-server и общается с ним через
# asyncio), но внутри себя не стримит: собирает кандидатов, прогоняет
# через агента одним вызовом и возвращает готовый список. Поэтому
# обёртка в generator ничего не теряет — тот же паттерн, что и для
# stream_kaggle_articles() в mcp_fetcher_kaggle.py.
def stream_hf_articles(query: str = "text-generation"):
    """Синхронный генератор-адаптер над fetch_hf_materials_via_mcp()."""
    articles = asyncio.run(fetch_hf_materials_via_mcp(query))
    for article in articles:
        yield article


# === БЛОК ДЛЯ ТЕСТИРОВАНИЯ ИЗОЛИРОВАННО (ПРИ ПРЯМОМ ЗАПУСКЕ ФАЙЛА) ===
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s")
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    print("=== ТЕСТОВЫЙ ЗАПУСК МОДУЛЯ ===")
    res = asyncio.run(fetch_hf_materials_via_mcp("LLM"))
    print(f"\nПолучено материалов: {len(res)}\n")
    print(json.dumps(res, indent=2, ensure_ascii=False))