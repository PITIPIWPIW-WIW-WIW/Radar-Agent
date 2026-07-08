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

# Изолированный импорт
try:
    from pydantic_ai import Agent
    from agent_manager import get_model
except ImportError:
    Agent = None
    def get_model(): return None

load_dotenv()
logger = logging.getLogger("hf_datasets_fetcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# === ПЕРЕКЛЮЧАТЕЛЬ РЕЖИМА ===
IS_TEST_MODE = True


# === 1. СХЕМЫ ДЛЯ ИИ-ПЕРЕВОДЧИКА ===
class SelectedDataset(BaseModel):
    id: str = Field(description="ID выбранного датасета (например, hf_dataset:databricks/databricks-dolly-15k)")
    russian_translation: str = Field(
        description="Подробный, технически грамотный перевод описания датасета на русский язык. Укажи объем, структуру и назначение."
    )

class SelectionResult(BaseModel):
    selected_datasets: list[SelectedDataset] = Field(
        description="Список (до 5) отобранных датасетов с их переводом"
    )

# === 1.1 МОКИ ДЛЯ ТЕСТИРОВАНИЯ ===
class MockRunResult:
    def __init__(self, output_data: SelectionResult):
        self.output = output_data

class MockAgent:
    async def run(self, prompt: str) -> MockRunResult:
        logger.info("[MOCK] Агент получил промпт для датасетов. Имитация ответа (1 сек)...")
        await asyncio.sleep(1)
        
        test_id = "hf_dataset:dummy/test-dataset"
        try:
            json_str = prompt.split("Кандидаты для анализа и перевода:\n")[-1]
            candidates = json.loads(json_str)
            if candidates:
                test_id = candidates[0]["id"]
        except Exception:
            pass

        mock_data = SelectionResult(
            selected_datasets=[
                SelectedDataset(
                    id=test_id,
                    russian_translation="[МОК-ПЕРЕВОД] Это тестовый датасет на 10 тысяч строк. Содержит колонки 'text' и 'label', отлично подходит для fine-tuning."
                )
            ]
        )
        return MockRunResult(mock_data)

_hf_dataset_agent = None

def _get_hf_dataset_agent():
    global _hf_dataset_agent
    if IS_TEST_MODE:
        return MockAgent()
        
    if _hf_dataset_agent is None:
        _hf_dataset_agent = Agent(
            model=get_model(),
            output_type=SelectionResult,
            system_prompt=(
                "Ты — Data Scientist. Тебе дают JSON со списком новых датасетов с платформы Hugging Face.\n"
                "ТВОЯ ЗАДАЧА: Отбери до 5 самых качественных датасетов, переведи их суть на русский (объем, формат данных, для чего нужны) и верни JSON."
            )
        )
    return _hf_dataset_agent


# === 2. НАСТРОЙКИ СЕРВЕРА MCP ===
HF_SERVER_PARAMS = StdioServerParameters(
    command="uvx",
    args=["huggingface-mcp-server"],
    env={
        "HUGGINGFACE_API_KEY": os.getenv("HUGGINGFACE_API_KEY", ""),
        "PATH": os.getenv("PATH", "")
    }
)


# === 3. ФУНКЦИИ ОЧИСТКИ И СБОРА ===
def clean_hf_readme(text: str) -> str:
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.DOTALL)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", "", text)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    text = re.sub(r"^[#]+\s+", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "").replace("*", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

async def fetch_dataset_readme_directly(dataset_id: str) -> str:
    clean_id = dataset_id.strip()
    # ВАЖНО: Путь изменен на /datasets/
    url = f"https://huggingface.co/datasets/{clean_id}/raw/main/README.md"
    
    proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("ALL_PROXY")
    if proxy_url:
        proxy_url = proxy_url.strip()
        if not proxy_url.startswith(("http://", "https://")):
            proxy_url = f"http://{proxy_url}"
            
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False, proxy=proxy_url) as client:
            response = await client.get(url, follow_redirects=True)
            if response.status_code == 200:
                return response.text
            else:
                logger.warning(f"HuggingFace вернул статус {response.status_code} для датасета {clean_id}")
    except httpx.RequestError as e:
        logger.error(f"Ошибка сети при скачивании датасета {clean_id}: {e}")
        
    return ""

async def _call_tool(session: ClientSession, tool_name: str, arguments: dict) -> str:
    response = await session.call_tool(tool_name, arguments=arguments)
    if not response.content:
        return ""
    return "\n".join(part.text for part in response.content if hasattr(part, "text") and part.text)


# === 4. ГЛАВНАЯ ЛОГИКА ===
async def fetch_hf_datasets_via_mcp(query: str = "machine learning") -> list[dict]:
    articles_payload = []
    
    if not os.getenv("HUGGINGFACE_API_KEY"):
        logger.warning("HUGGINGFACE_API_KEY не задан.")

    logger.info("Запуск подпроцесса Hugging Face MCP (Datasets)...")
    try:
        async with stdio_client(HF_SERVER_PARAMS) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                
                # Шаг 1: Поиск датасетов
                search_args = {"query": query, "limit": 15}
                raw_list = await _call_tool(session, "search-datasets", search_args)
                datasets_data = json.loads(raw_list)
                
                all_candidates = []
                for dataset in datasets_data:
                    dataset_id = dataset.get("id")
                    if not dataset_id: continue
                    
                    # Шаг 2: Скачивание
                    raw_readme = await fetch_dataset_readme_directly(dataset_id)
                    if not raw_readme: continue
                        
                    # Шаг 3: Очистка
                    clean_text = clean_hf_readme(raw_readme)
                    if len(clean_text) < 50: continue
                        
                    all_candidates.append({
                        "id": f"hf_dataset:{dataset_id}",
                        "title": dataset_id,
                        "full_text": clean_text,
                        "url": f"https://huggingface.co/datasets/{dataset_id}"
                    })
                
                if not all_candidates:
                    logger.info("Кандидаты не найдены.")
                    return articles_payload
                
                freshest_candidates = all_candidates[:10]
                candidates_by_id = {c["id"]: c for c in freshest_candidates}
                
                llm_input = [
                    {"id": c["id"], "title": c["title"], "full_text": c["full_text"][:2500]} 
                    for c in freshest_candidates
                ]
                
                # Шаг 4: Агент
                start_time = time.perf_counter()
                prompt = "Кандидаты для анализа и перевода:\n" + json.dumps(llm_input, ensure_ascii=False)
                
                agent = _get_hf_dataset_agent()
                result = await agent.run(prompt)
                
                # Шаг 5: Форматирование результата
                for item in result.output.selected_datasets:
                    original = candidates_by_id.get(item.id)
                    if not original: continue
                        
                    articles_payload.append({
                        "title": f"[HF Dataset] {original['title']}",
                        "text": f"Датасет: {original['title']}\n\nОписание:\n{item.russian_translation}",
                        "source_url": original["url"],
                    })
                    
                logger.info("Агент завершил работу за %.2fs", time.perf_counter() - start_time)

    except Exception as e:
        logger.error(f"Сбой в пайплайне Hugging Face Datasets: {e}")
        
    return articles_payload


# === СИНХРОННАЯ ОБЁРТКА ПОД КОНТРАКТ stream_all_new_articles() ===
def stream_hf_datasets(query: str = "machine learning"):
    """Синхронный генератор-адаптер над fetch_hf_datasets_via_mcp()."""
    articles = asyncio.run(fetch_hf_datasets_via_mcp(query))
    for article in articles:
        yield article


if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    print("=== ТЕСТОВЫЙ ЗАПУСК МОДУЛЯ ДАТАСЕТОВ ===")
    res = asyncio.run(fetch_hf_datasets_via_mcp("finance"))
    print(f"\n✅ Получено датасетов: {len(res)}\n")
    print(json.dumps(res, indent=2, ensure_ascii=False))