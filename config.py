import os
from dotenv import load_dotenv

load_dotenv()

# --- Mistral API ---
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_MODEL_NAME = os.getenv("MISTRAL_MODEL_NAME", "mistral-small-latest")

if not MISTRAL_API_KEY:
    raise RuntimeError(
        "MISTRAL_API_KEY не найден. Создай файл .env на основе .env.example "
        "и укажи там свой ключ."
    )

# --- Дедупликация / векторы ---
DUPLICATE_THRESHOLD = float(os.getenv("DUPLICATE_THRESHOLD", "0.92"))
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "384"))

# --- Логирование ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "app.log")
ERROR_LOG_FILE = os.getenv("ERROR_LOG_FILE", "errors.log")