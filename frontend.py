#для запуска в первой консоли uvicorn app:app --reload, во второй python -m streamlit run frontend.py
import streamlit as st
import requests
import pandas as pd

st.set_page_config(page_title="AI Dashboard", layout="wide", initial_sidebar_state="expanded")

custom_css = """
<style>
    /* Скрываем верхнюю полосу */
    header {visibility: hidden !important;}
    .block-container {padding-top: 1.5rem !important; max-width: 96% !important;}

    /* === ВОЗВРАЩАЕМ И КРАСИВО СТИЛИЗУЕМ КНОПКУ ОТКРЫТИЯ САЙДБАРА === */
    [data-testid="collapsedControl"] {
        visibility: visible !important;
        background-color: #0a0f1c !important; /* Темный фон */
        border-right: 2px solid #10b981 !important; /* Зеленая рамка справа */
        border-top: 2px solid #10b981 !important;
        border-bottom: 2px solid #10b981 !important;
        border-radius: 0 10px 10px 0 !important; /* Закругляем только правые углы */
        top: 15px !important; /* Смещаем чуть ниже верхнего края */
        box-shadow: 0 4px 15px rgba(0, 0, 0, 0.6) !important;
        transition: all 0.3s ease !important;
        z-index: 999999 !important;
    }
    
    /* Свечение при наведении на кнопку открытия */
    [data-testid="collapsedControl"]:hover {
        box-shadow: 0 0 15px rgba(16, 185, 129, 0.8) !important;
        transform: scale(1.05) !important;
    }
    
    /* Делаем саму иконку стрелочки зеленой */
    [data-testid="collapsedControl"] svg {
        color: #10b981 !important;
        fill: #10b981 !important;
    }

    /* === СТИЛИЗУЕМ КНОПКУ ЗАКРЫТИЯ САЙДБАРА ВНУТРИ ПАНЕЛИ === */
    [data-testid="stSidebar"] button {
        color: #10b981 !important;
    }
    [data-testid="stSidebar"] button:hover {
        background-color: rgba(16, 185, 129, 0.1) !important;
    }
    [data-testid="stSidebar"] button svg {
        fill: #10b981 !important;
    }

    /* 1. ПРИНУДИТЕЛЬНЫЙ ЧЕРНЫЙ ФОН ВСЕГО ПРИЛОЖЕНИЯ И САЙДБАРА */
    [data-testid="stAppViewContainer"] {
        background-color: #050810 !important; 
    }
    [data-testid="stSidebar"] {
        background-color: #080c17 !important;
        border-right: 1px solid #10b981 !important; /* Зеленая полоса отделения сайдбара */
    }

    /* Принудительно делаем текст светлым, чтобы его было видно на черном фоне */
    p, span, h1, h2, h3, h4, h5, h6, label, li {
        color: #f8fafc !important;
    }

    /* 2. АГРЕССИВНЫЕ ЗЕЛЕНЫЕ РАМКИ ДЛЯ КАРТОЧЕК */
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 12px !important;
        border: 2px solid #10b981 !important; /* Яркий зеленый цвет Arena.ai */
        background-color: #0a0f1c !important; /* Чуть светлее фона для объема */
        box-shadow: 0 4px 10px rgba(0, 0, 0, 0.8) !important;
        transition: all 0.3s ease !important;
    }

    /* Зеленое неоновое свечение при наведении */
    [data-testid="stVerticalBlockBorderWrapper"]:hover {
        transform: translateY(-5px) !important;
        border-color: #34d399 !important; /* Более светлый зеленый */
        box-shadow: 0 0 25px rgba(16, 185, 129, 0.6) !important; /* Яркое свечение */
    }

    /* 3. СТИЛИЗАЦИЯ КНОПОК В СТИЛЕ ARENA.AI */
    div.stButton > button {
        border-radius: 8px !important;
        font-weight: 700 !important;
        border: 1px solid #10b981 !important;
        background-color: transparent !important;
        color: #10b981 !important;
        transition: all 0.2s ease !important;
    }

    /* Заливка зеленом при наведении на обычную кнопку */
    div.stButton > button:hover {
        background-color: #10b981 !important;
        color: #050810 !important; /* Текст становится темным */
        box-shadow: 0 0 15px rgba(16, 185, 129, 0.5) !important;
        transform: scale(1.02);
    }

    /* Главная кнопка (сразу залита зеленым) */
    div.stButton > button[kind="primary"] {
        background-color: #10b981 !important; 
        color: #050810 !important;
        border: none !important;
    }
    div.stButton > button[kind="primary"]:hover {
        background-color: #34d399 !important;
        box-shadow: 0 0 25px rgba(16, 185, 129, 0.8) !important;
    }

    /* 4. ЦИФРЫ РЕЙТИНГА */
    [data-testid="stMetricValue"] {
        font-weight: 900 !important;
        font-size: 2.5rem !important;
        color: #10b981 !important; /* Зеленый текст рейтинга */
        text-shadow: 0 0 10px rgba(16, 185, 129, 0.3) !important;
    }

    /* Зеленые разделители (hr) */
    hr {
        border-color: rgba(16, 185, 129, 0.3) !important;
    }
</style>
"""
st.markdown(custom_css, unsafe_allow_html=True)


def fetch_leaderboard():
    try:
        response = requests.get("http://localhost:8000/leaderboard", timeout=5)
        if response.status_code == 200:
            return response.json()
    except requests.exceptions.ConnectionError:
        try:
            response = requests.get("http://127.0.0.1:8000/leaderboard", timeout=5)
            if response.status_code == 200:
                return response.json()
        except requests.exceptions.ConnectionError:
            return {"status": "connect_error", "data": []}
    return {"status": "error", "message": "Ошибка сервера", "data": []}


def fetch_articles():
    try:
        response = requests.get("http://localhost:8000/articles", timeout=5)
        if response.status_code == 200:
            return response.json()
    except requests.exceptions.ConnectionError:
        try:
            response = requests.get("http://127.0.0.1:8000/articles", timeout=5)
            if response.status_code == 200:
                return response.json()
        except requests.exceptions.ConnectionError:
            return {"status": "connect_error", "data": []}
    return {"status": "error", "message": "Ошибка сервера", "data": []}


st.sidebar.title("Меню приложения")
page = st.sidebar.radio(
    "Выберите раздел:",
    ["Лидерборд моделей", "База статей"]
)
st.sidebar.divider()

if page == "Лидерборд моделей":
    st.title("История снимков лидерборда Arena.ai")

    col_refresh, col_scrape = st.columns([1, 3])
    with col_refresh:
        st.button("Обновить дашборд", use_container_width=True)
    with col_scrape:
        if st.button("Запустить живой сбор данных с Arena.ai", type="primary", use_container_width=True):
            try:
                try:
                    res = requests.post("http://localhost:8000/scrape", timeout=5).json()
                except requests.exceptions.ConnectionError:
                    res = requests.post("http://127.0.0.1:8000/scrape", timeout=5).json()

                if res.get("status") == "success":
                    st.info(
                        "Утвержденный парсер запущен на бэкенде в фоновом режиме! Подождите около 1.5 минут и обновите дашборд.")
                else:
                    st.error("Не удалось запустить парсер.")
            except Exception as e:
                st.error(f"Ошибка запроса к бэкенду: {e}")

    api_response = fetch_leaderboard()

    if api_response.get("status") == "success" and api_response.get("data"):
        snapshots = api_response["data"]
        leaderboard_records = []

        for snapshot in snapshots:
            raw_fetched_at = snapshot.get("fetched_at", "Неизвестная дата")
            if "T" in raw_fetched_at:
                date_part, time_part = raw_fetched_at.split("T")
                fetched_at = f"{date_part} {time_part.split('.')[0]}"
            else:
                fetched_at = raw_fetched_at

            categories = snapshot.get("categories", {})
            for category_name, models_list in categories.items():
                for model in models_list:
                    leaderboard_records.append({
                        "fetched_at": fetched_at,
                        "category": category_name.strip(),
                        "model_name": model.get("name", "Без имени").strip(),
                        "rating": model.get("rating", 0)
                    })

        st.sidebar.header("Панель сравнения моделей")

        unique_categories = sorted(list(set(r["category"] for r in leaderboard_records)))
        selected_category = st.sidebar.selectbox("Категория:", ["Все категории"] + unique_categories)

        unique_dates = sorted(list(set(r["fetched_at"] for r in leaderboard_records)), reverse=True)
        selected_date = st.sidebar.selectbox("Дата снимка:", ["Все даты"] + unique_dates)

        search_query = st.sidebar.text_input("Поиск модели:")

        filtered_records = leaderboard_records
        if selected_category != "Все категории":
            filtered_records = [r for r in filtered_records if r["category"] == selected_category]
        if selected_date != "Все даты":
            filtered_records = [r for r in filtered_records if r["fetched_at"] == selected_date]
        if search_query:
            filtered_records = [r for r in filtered_records if search_query.lower() in r["model_name"].lower()]

        filtered_records.sort(key=lambda x: x['rating'], reverse=True)

        st.write("---")
        st.subheader(f"Результаты сравнения ({len(filtered_records)} моделей)")

        if filtered_records:
            display_mode = st.radio("Формат отображения:", ["Вертикальный список", "Таблица"], horizontal=True)

            if display_mode == "Вертикальный список":
                for idx, record in enumerate(filtered_records):
                    with st.container(border=True):
                        col_rank, col_info, col_rating = st.columns([1, 5, 2])
                        with col_rank:
                            if idx == 0:
                                st.markdown("### 🥇 **1**")
                            elif idx == 1:
                                st.markdown("### 🥈 **2**")
                            elif idx == 2:
                                st.markdown("### 🥉 **3**")
                            else:
                                st.markdown(f"### **#{idx + 1}**")
                        with col_info:
                            st.markdown(f"#### **{record['model_name']}**")
                            st.caption(
                                f"Категория: **{record['category'].upper()}** | Снимок: **{record['fetched_at']}**")
                        with col_rating:
                            st.metric(label="Рейтинг (Elo)", value=f"{record['rating']}")
            else:
                df = pd.DataFrame(filtered_records)
                df.insert(0, 'Место', range(1, len(df) + 1))
                df.columns = ["Место", "Дата снимка", "Категория", "Название модели", "Рейтинг (pts)"]
                st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.warning("По выбранным критериям модели не найдены.")

    elif api_response.get("status") == "connect_error":
        st.error("FastAPI бэкенд не отвечает. Убедись, что запущен uvicorn.")
    else:
        st.warning("В базе данных пока нет снимков Арены.")


elif page == "База статей":
    st.title("Сохраненные статьи")

    st.button("Обновить статьи", use_container_width=True)

    api_response = fetch_articles()

    if api_response.get("status") == "success" and api_response.get("data"):
        articles = api_response["data"]

        st.sidebar.header("Фильтры статей")

        all_tags = set()
        for a in articles:
            tags = a.get('tags', [])
            if isinstance(tags, list):
                for t in tags:
                    all_tags.add(t.strip().lower())
        unique_tags = sorted(list(all_tags))

        unique_dates = sorted(list(set(
            a.get('added_at', '').split(' ')[0] for a in articles if a.get('added_at')
        )), reverse=True)

        selected_tag = st.sidebar.selectbox("Выберите тег:", ["Все теги"] + unique_tags)
        selected_article_date = st.sidebar.selectbox("Дата добавления:", ["Все даты"] + unique_dates)
        search_article = st.sidebar.text_input("Поиск по тексту:")

        filtered_articles = articles

        if selected_tag != "Все теги":
            filtered_articles = [a for a in filtered_articles if
                                 selected_tag in [t.strip().lower() for t in a.get('tags', [])]]

        if selected_article_date != "Все даты":
            filtered_articles = [a for a in filtered_articles if
                                 a.get('added_at', '').startswith(selected_article_date)]

        if search_article:
            search_lower = search_article.lower()
            filtered_articles = [
                a for a in filtered_articles
                if search_lower in a.get('title', '').lower() or search_lower in a.get('summary', '').lower()
            ]

        filtered_articles.sort(key=lambda x: x.get('added_at', ''), reverse=True)

        st.write("---")
        st.subheader(f"Результаты поиска ({len(filtered_articles)} статей)")

        if filtered_articles:
            cols_per_row = 2
            for i in range(0, len(filtered_articles), cols_per_row):
                row_articles = filtered_articles[i: i + cols_per_row]
                cols = st.columns(cols_per_row)

                for j, article in enumerate(row_articles):
                    with cols[j]:
                        with st.container(border=True):
                            st.subheader(article.get('title', 'Без заголовка'))

                            source = article.get('source_url', '#')
                            date_str = article.get('added_at', '')
                            st.caption(f"{date_str} | [Переход к источнику]({source})")

                            st.divider()
                            st.write(article.get('summary', 'Нет описания'))

                            tags = article.get('tags', [])
                            if tags:
                                st.markdown(f"`{', '.join(tags)}`")
        else:
            st.warning("По вашим фильтрам статьи не найдены.")

    elif api_response.get("status") == "connect_error":
        st.error("FastAPI бэкенд не отвечает. Убедись, что запущен uvicorn.")
    else:
        st.warning("В базе данных пока нет сохраненных статей.")