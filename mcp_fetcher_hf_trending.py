import asyncio
import logging
import os
import re

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("hf_trending_fetcher")

HF_TRENDING_MCP_PATH = os.getenv("HF_TRENDING_MCP_PATH", "")

TRENDING_SERVER_PARAMS = StdioServerParameters(
    command="uv",
    args=["--directory", HF_TRENDING_MCP_PATH, "run", "main.py"],
)

_MODEL_LINE_RE = re.compile(
    r"^(?P<name>\S+)\s+\(Downloads:\s*(?P<downloads>[\d,]+),\s*Likes:\s*(?P<likes>[\d,]+)\)\s*$"
)
_TAGS_LINE_RE = re.compile(r"^Tags:\s*(?P<tags>.*)$")


def _parse_trending_text(raw_text: str) -> list[dict]:
    models = []
    current = None

    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue

        model_match = _MODEL_LINE_RE.match(line)
        if model_match:
            if current:
                models.append(current)
            current = {
                "name": model_match.group("name"),
                "downloads": int(model_match.group("downloads").replace(",", "")),
                "likes": int(model_match.group("likes").replace(",", "")),
                "tags": [],
            }
            continue

        tags_match = _TAGS_LINE_RE.match(line)
        if tags_match and current is not None:
            tags_str = tags_match.group("tags").strip()
            current["tags"] = [t.strip() for t in tags_str.split(",") if t.strip()]

    if current:
        models.append(current)

    return models


async def _call_get_trending_models(limit: int) -> str:
    if not HF_TRENDING_MCP_PATH:
        raise RuntimeError(
            "HF_TRENDING_MCP_PATH не задан в .env — укажи путь к папке, "
            "куда склонирован https://github.com/kukapay/hf-trending-mcp"
        )

    async with stdio_client(TRENDING_SERVER_PARAMS) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            response = await session.call_tool("get_trending_models", arguments={"limit": limit})
            if not response.content:
                return ""
            return "\n".join(
                part.text for part in response.content if hasattr(part, "text") and part.text
            )


def fetch_trending_models(limit: int = 15) -> list[dict]:
    try:
        raw_text = asyncio.run(_call_get_trending_models(limit))
    except Exception as e:
        logger.error(f"Не удалось получить трендовые модели HF: {e}")
        return []

    if not raw_text:
        return []

    return _parse_trending_text(raw_text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("ТЕСТОВЫЙ ЗАПУСК: трендовые модели HF")
    result = fetch_trending_models(5)
    print(f"Получено: {len(result)}\n")
    for m in result:
        print(m)