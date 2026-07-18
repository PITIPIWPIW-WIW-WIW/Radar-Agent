import datetime
import json
import logging
import time
import random
from playwright.sync_api import sync_playwright

from database import init_db, save_leaderboard_data
from leaderboard_analyzer import run_leaderboard_analysis

logger = logging.getLogger("arena_scraper")

# ==========================================
# ЧАСТЬ 1: СКРАПЕР (Получение HTML-кода)
# ==========================================
def scrape(page, url):
    """
    Использует уже готовую страницу (page) для перехода по ссылке.
    Дожидается загрузки таблицы и обрабатывает куки.
    """
    print(f"Открываем страницу: {url}")
    # Переходим по URL и ждем базовой загрузки DOM-дерева
    page.goto(url, wait_until="domcontentloaded")

    print("Ожидаем загрузку таблицы...")
    try:
        # Ждем появления тега <table> максимум 20 секунд
        page.wait_for_selector("table", timeout=20000)
        print("Таблица успешно обнаружена на странице!")
    except Exception as e:
        # Если таблица не появилась (из-за жесткой капчи или долгой загрузки)
        raise Exception("Таблица не появилась за отведенное время!") from e

    # --- ОБРАБОТКА БАННЕРА КУКИ ---
    try:
        # Ищем кнопку принятия куки по нескольким вариантам текста —
        # разные баннеры (OneTrust, Cookiebot и т.п.) формулируют её по-разному
        for label in ["Accept", "Accept All", "I Accept", "Agree", "Got it"]:
            accept_button = page.locator("button", has_text=label).first
            if accept_button.is_visible(timeout=1000):
                accept_button.click()
                page.wait_for_timeout(500)  # Небольшая пауза после клика
                break
    except:
        pass

    # --- ПРОВЕРКА НА БЛОКИРОВКИ ---
    html_content = page.content().lower()
    block_markers = ["verify you are human", "just a moment", "checking your browser"]
    for marker in block_markers:
        if marker in html_content:
            raise Exception(f"Сайт неожиданно выдал защиту. Маркер: {marker}")


# ==========================================
# ЧАСТЬ 2: ПАРСЕР (Извлечение данных)
# ==========================================
def parse(page) -> list[dict]:
    """
    Ориентируется строго на 11 основных категорий.
    Сам находит, в каком столбце лежит "Model", а в каком "Score" / "Rating".
    """
    table = page.locator("table").first
    table.locator("tbody tr").first.wait_for(timeout=10000)

    # 1. Анализируем шапку таблицы (находим нужные столбцы по названиям)
    headers = table.locator("thead th").all()

    model_idx = 1  # Дефолтные значения на случай непредвиденных ситуаций
    score_idx = 2

    for i, header in enumerate(headers):
        text = header.inner_text().lower()
        if "model" in text:
            model_idx = i
        elif "score" in text or "rating" in text or "elo" in text:
            score_idx = i

    # 2. Собираем данные из строк по найденным индексам
    rows = table.locator("tbody tr").all()
    top_5_rows = rows[:5]
    parsed_data = []

    for index, row in enumerate(top_5_rows):
        try:
            cells = row.locator("td").all()

            # Проверка от ошибок сдвига
            if len(cells) <= max(model_idx, score_idx):
                continue

            raw_name = cells[model_idx].inner_text().strip()
            raw_rating = cells[score_idx].inner_text().strip()

            # --- ОЧИСТКА ДАННЫХ ---
            # Отсекаем разработчика (всё, что после переноса строки)
            clean_name = raw_name.split('\n')[0].strip()

            # Очищаем цифры (убираем % и погрешности)
            clean_rating_str = raw_rating.split('\n')[0].strip().replace('%', '')

            try:
                if '.' in clean_rating_str:
                    clean_rating = float(clean_rating_str)
                else:
                    clean_rating = int(clean_rating_str)
            except ValueError:
                clean_rating = clean_rating_str

            if clean_name:
                row_dict = {"name": clean_name, "rating": clean_rating}
                parsed_data.append(row_dict)

        except Exception as e:
            print(f"[ПРЕДУПРЕЖДЕНИЕ] Ошибка парсинга строки {index + 1}: {e}")
            continue

    return parsed_data


# ==========================================
# ЧАСТЬ 3: КООРДИНАТОР И ЗАПУСК
# ==========================================
def collect_all_categories(category_urls):
    """
    Создает ЕДИНУЮ сессию браузера для обхода Cloudflare с нативным Stealth.
    Проходит по всем категориям с человекоподобными задержками.
    """
    fetched_at = datetime.datetime.now().isoformat()
    all_data = {}

    with sync_playwright() as playwright:
        # 1. Запуск браузера с нативными аргументами маскировки автоматизации
        browser = playwright.chromium.launch(
            headless=True,
            channel="chrome",  # настоящий установленный Chrome, а не bundled Chromium —
                                # Cloudflare куда охотнее банит именно голый Chromium
            args=[
                "--disable-blink-features=AutomationControlled",  # Отключает главный маркер робота navigator.webdriver
                "--no-sandbox",
                "--disable-infobars",
                "--window-position=0,0",
                "--ignore-certificate-errors"
            ]
        )

        # 2. Создаем контекст с реальными параметрами пользователя (User-Agent, Язык, Экран)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="ru-RU",
            timezone_id="Europe/Moscow"
        )
        page = context.new_page()

        # 3. Глубокий JS-Stealth: Внедряем подмену отпечатков до загрузки любого сайта
        #
        # ВАЖНО: комментарии в этом блоке — JavaScript, не Python! Использованы
        # правильные "//"-комментарии. В исходной версии тут стояли Python-style
        # "#"-комментарии внутри JS-строки, что является синтаксической ошибкой
        # в JS — скрипт не мог выполниться в браузере, и вся stealth-маскировка
        # (ради которой этот блок и писался) фактически не срабатывала.
        page.add_init_script("""
            // Убираем следы автоматизации на уровне navigator
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // Эмулируем реальные плагины браузера (у ботов их обычно 0)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [{ name: 'PDF Viewer' }, { name: 'Chrome PDF Viewer' }, { name: 'Chromium PDF Viewer' }]
            });

            // Подменяем языки и платформу
            Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU', 'ru', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

            // Добавляем стандартный объект window.chrome, который Cloudflare часто проверяет
            window.chrome = {
                runtime: {},
                loadTimes: Date.now,
                csi: () => {},
                app: {}
            };

            // Маскируем параметры видеокарты (WebGL), чтобы не выдавать дефолтный headless-движок
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Open Source Technology Center';
                if (parameter === 37446) return 'Intel(R) Iris(R) Xe Graphics x86/MMX/SSE2';
                return getParameter.apply(this, arguments);
            };
        """)

        # 4. Обход ссылок в рамках единой сессии
        for category, url in category_urls.items():
            print(f"\n--- Собираем данные для категории: {category} ---")

            try:
                scrape(page, url)
                parsed_list = parse(page)
                all_data[category] = parsed_list
            except Exception as e:
                print(f"[ОШИБКА] Не удалось собрать {category}: {e}")
                logger.error(f"Не удалось собрать категорию '{category}' ({url}): {e!r}")
                # Один повтор: антибот-челлендж часто флакует (проходит со второго
                # раза), а не блокирует намертво — не сдаёмся с первой попытки.
                print(f"[ПОВТОР] Пробуем категорию {category} ещё раз через паузу...")
                time.sleep(random.uniform(5, 10))
                try:
                    scrape(page, url)
                    parsed_list = parse(page)
                    all_data[category] = parsed_list
                    print(f"[ПОВТОР УСПЕШЕН] {category} собрана со второй попытки.")
                except Exception as e2:
                    print(f"[ОШИБКА] Повтор для {category} тоже провалился: {e2}")
                    logger.error(f"Повтор для '{category}' тоже провалился: {e2!r}")

            # --- ЧЕЛОВЕКОПОДОБНАЯ ЗАДЕРЖКА ---
            # Случайная пауза от 3.5 до 7.5 секунд
            delay = random.uniform(3.5, 7.5)
            print(f"Ожидание {delay:.2f} сек. для имитации действий человека...")
            time.sleep(delay)

        print("\nЗакрываем браузер...")
        browser.close()

    failed = [c for c in category_urls if c not in all_data]
    if failed:
        logger.error(
            f"Скрапинг арены: {len(failed)}/{len(category_urls)} категорий "
            f"провалились: {', '.join(failed)}"
        )
    if not all_data:
        # Раньше в этом случае функция молча возвращала пустой снимок, save_to_db()
        # его молча сохранял (0 строк), а вызывающий код (main.py:_refresh_leaderboard)
        # никогда не видел исключения — отсюда пустые таблицы leaderboard* без единой
        # строки в логах. Явно роняем пайплайн, если не собралась ВООБЩЕ ни одна категория.
        raise RuntimeError(
            "Все категории лидерборда провалились — вероятно, антибот-защита "
            "(Cloudflare) блокирует скрапер. Снимок не сохранён."
        )

    final_snapshot = {
        "fetched_at": fetched_at,
        "categories": all_data
    }

    return final_snapshot

def save_to_db(data):
    # init_db() создаёт таблицы (включая leaderboard), если их ещё нет —
    # безопасно вызывать даже если main.py ещё не запускался и БД не создана.
    init_db()
    save_leaderboard_data(data)
    print(f"\n[УСПЕХ] Сохранено в БД: {sum(len(v) for v in data['categories'].values())} строк "
          f"по {len(data['categories'])} категориям (снимок от {data['fetched_at']})")

    print("[АНАЛИЗ] Строим инкрементальный анализ (новый снимок + предыдущий + предыдущий анализ)...")
    run_leaderboard_analysis()
    print("[АНАЛИЗ] Готово.")

# Единый источник правды для списка категорий арены — раньше этот же словарь
# был продублирован ещё и в app.py (run_original_scraper_and_save), теперь
# оба места импортируют отсюда.
CATEGORY_URLS = {
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


def main():
    print(f"Запуск скрапера для {len(CATEGORY_URLS)} категорий...")

    data = collect_all_categories(CATEGORY_URLS)
    save_to_db(data)
    print("\n[УСПЕХ] Сбор данных завершен!")

if __name__ == "__main__":
    main()