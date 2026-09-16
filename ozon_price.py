#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ozon + Wildberries Price Checker
--------------------------------
Утилита: мониторит цены товаров Ozon и Wildberries по двум отдельным спискам
артикулов и при снижении цены отправляет уведомление в Telegram. Управление —
через кнопки бота.

Использование:
    python ozon_price.py

Требования:
    pip install selenium selenium-stealth requests aiogram
    Установленный Google Chrome (версия должна совпадать с chromedriver,
    Selenium 4.15+ обычно подтягивает драйвер автоматически).

Файлы, которые программа создаёт и ведёт сама:
    articles_ozon.txt      — список отслеживаемых артикулов Ozon
    articles_wb.txt        — список отслеживаемых артикулов Wildberries
    price_state.json       — данные предыдущего цикла по каждому артикулу (обеих площадок)
    telegram_config.txt    — токен бота и chat_id (нужно заполнить один раз)
"""

import sys
import os
import json
import re
import time
import asyncio
import threading
import logging
from pathlib import Path
from typing import Optional, Dict, List

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import WebDriverException, TimeoutException
from selenium_stealth import stealth

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("ozon_price")

# Тот же внутренний JSON-эндпоинт, которым пользуется сам сайт для подгрузки данных
API_URL_TEMPLATE = "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/product/{article}&__rr=1"

BLOCKED_INDICATORS = [
    "cloudflare", "checking your browser", "enable javascript",
    "access denied", "blocked", "ddos-guard", "проверка браузера",
    "доступ ограничен", "access restricted",
]

# ANSI-коды для подсветки снижения цены ярко-зелёным в консоли
COLOR_GREEN = "\033[92m"
COLOR_RESET = "\033[0m"


def enable_ansi_colors() -> None:
    """На Windows консоль по умолчанию может не понимать ANSI-коды цвета — включаем их принудительно."""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


def create_driver(headless: bool = True) -> webdriver.Chrome:
    """Создаёт Chrome-драйвер с настройками, снижающими вероятность детекта автоматизации."""
    options = Options()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")
    options.add_argument("--window-size=1920,1080")

    if headless:
        options.add_argument("--headless")

    driver = webdriver.Chrome(options=options)

    stealth(
        driver,
        languages=["ru-RU", "ru"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )

    driver.implicitly_wait(20)
    driver.set_page_load_timeout(60)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    return driver


def is_blocked(driver: webdriver.Chrome) -> bool:
    """Проверяет, показывает ли страница признаки антибот-блокировки."""
    try:
        page_source = driver.page_source.lower()
        return any(indicator in page_source for indicator in BLOCKED_INDICATORS)
    except Exception:
        return True


def wait_for_antibot_bypass(driver: webdriver.Chrome, max_wait_time: int = 120) -> None:
    """Ждёт, пока страница пройдёт антибот-проверку, при необходимости перезагружая её."""
    start_time = time.time()
    reload_attempts = 0
    max_reload_attempts = 3

    while time.time() - start_time < max_wait_time:
        if is_blocked(driver):
            if reload_attempts < max_reload_attempts:
                reload_attempts += 1
                logger.info(
                    f"Обнаружена блокировка, перезагрузка страницы "
                    f"(попытка {reload_attempts}/{max_reload_attempts})"
                )
                driver.refresh()
                time.sleep(10)
                continue
            raise Exception("Access blocked after retries")
        return

    raise Exception("Antibot timeout")


def extract_json_from_html(html_content: str) -> Optional[str]:
    """Достаёт JSON-содержимое либо из <pre> тега, либо по первой/последней фигурной скобке."""
    pre_match = re.search(r"<pre[^>]*>(.*?)</pre>", html_content, re.DOTALL | re.IGNORECASE)
    if pre_match:
        return pre_match.group(1).strip()

    first_brace = html_content.find("{")
    last_brace = html_content.rfind("}")
    if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
        return html_content[first_brace:last_brace + 1]

    return None


def wait_for_json_response(driver: webdriver.Chrome, timeout: int = 60) -> Optional[str]:
    """Ждёт, пока страница отдаст валидный JSON с ключом widgetStates."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            json_content = extract_json_from_html(driver.page_source)
            if json_content:
                data = json.loads(json_content)
                if "widgetStates" in data:
                    return json_content
        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.debug(f"Ошибка при чтении страницы: {e}")

        time.sleep(2)

    return None


def find_price_widget(widget_states: Dict) -> Optional[Dict]:
    """Ищет виджет webPrice-* среди всех виджетов ответа."""
    for key, value in widget_states.items():
        if key.startswith("webPrice-") and isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                continue
    return None


def find_product_name(widget_states: Dict) -> str:
    """Достаёт название товара из webStickyProducts-*, если есть."""
    for key, value in widget_states.items():
        if key.startswith("webStickyProducts-") and isinstance(value, str):
            try:
                data = json.loads(value)
                return data.get("name", "")
            except json.JSONDecodeError:
                continue
    return ""


def extract_price_number(price_str: str) -> int:
    """Превращает строку вида '1 234 ₽' в число 1234."""
    if not price_str:
        return 0
    cleaned = re.sub(r"[^\d]", "", str(price_str))
    return int(cleaned) if cleaned else 0


def get_price_by_article(article: str, headless: bool = True) -> Dict:
    """
    Главная функция: возвращает словарь с ценой товара Ozon по артикулу.
    """
    max_driver_attempts = 3
    last_error = ""

    for driver_attempt in range(max_driver_attempts):
        driver = None
        try:
            logger.info(f"Драйвер #{driver_attempt + 1}/{max_driver_attempts}: запуск браузера")
            driver = create_driver(headless=headless)

            api_url = API_URL_TEMPLATE.format(article=article)
            logger.info(f"Переход по адресу API: {api_url}")
            driver.get(api_url)

            wait_for_antibot_bypass(driver)

            json_content = wait_for_json_response(driver)
            if not json_content:
                last_error = "Не удалось получить JSON-ответ от Ozon"
                logger.warning(last_error)
                continue

            data = json.loads(json_content)
            widget_states = data.get("widgetStates", {})

            price_widget = find_price_widget(widget_states)
            if not price_widget:
                last_error = "В ответе не найден виджет с ценой (возможно, товар недоступен или снят с продажи)"
                logger.warning(last_error)
                return {
                    "article": article,
                    "name": find_product_name(widget_states),
                    "price": 0,
                    "card_price": 0,
                    "original_price": 0,
                    "success": False,
                    "error": last_error,
                }

            result = {
                "article": article,
                "name": find_product_name(widget_states),
                "price": extract_price_number(price_widget.get("price", "")),
                "card_price": extract_price_number(price_widget.get("cardPrice", "")),
                "original_price": extract_price_number(price_widget.get("originalPrice", "")),
                "success": True,
                "error": "",
            }
            return result

        except Exception as e:
            last_error = str(e)
            if "Access blocked" in last_error or "Antibot timeout" in last_error:
                logger.warning(f"Драйвер #{driver_attempt + 1} заблокирован: {last_error}")
            else:
                logger.error(f"Ошибка на попытке #{driver_attempt + 1}: {last_error}")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass

    return {
        "article": article,
        "name": "",
        "price": 0,
        "card_price": 0,
        "original_price": 0,
        "success": False,
        "error": last_error or "Не удалось получить данные после нескольких попыток",
    }


def extract_article_from_input(raw: str) -> str:
    """Позволяет передавать как чистый артикул, так и полную ссылку на товар."""
    raw = raw.strip()
    match = re.search(r"/(?:product|catalog)/[^/]*?(\d+)/?", raw)
    if match:
        return match.group(1)
    # Поиск чистого блока цифр, если передана ссылка другого формата
    digits = re.findall(r"\d+", raw)
    if len(digits) == 1:
        return digits[0]
    return raw


# JSON-эндпоинт карточки товара Wildberries.
# В отличие от поиска WB он принимает уже известные артикулы и поддерживает
# несколько nmId в одном запросе.
WB_API_URL = "https://card.wb.ru/cards/v4/detail"
WB_DEST = -1257786
WB_BATCH_SIZE = 50

WB_REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}

WB_SESSION = requests.Session()
WB_SESSION.headers.update(WB_REQUEST_HEADERS)


def _rubles(value) -> int:
    """Безопасно переводит цену WB из копеек в рубли."""
    try:
        return int(round(int(value or 0) / 100))
    except (TypeError, ValueError):
        return 0


def _wb_error(article: str, message: str) -> dict:
    return {
        "article": article,
        "name": "",
        "price": 0,
        "card_price": 0,
        "original_price": 0,
        "success": False,
        "error": message,
    }


def parse_wb_product_data(product: dict) -> dict:
    """Извлекает товар и цену из ответа cards/v4/detail."""
    article = str(product.get("id", ""))
    name = product.get("name", "")

    prices = []
    for size in product.get("sizes") or []:
        price_info = size.get("price") or {}

        # В разных ответах WB встречаются обе схемы. В v4 итоговая цена —
        # total, а при отсутствии total: стоимость товара + логистика.
        raw_sale = price_info.get("total")
        if not raw_sale:
            raw_sale = (
                int(price_info.get("product") or 0)
                + int(price_info.get("logistics") or 0)
            )

        sale_price = _rubles(raw_sale)
        if sale_price:
            prices.append((sale_price, _rubles(price_info.get("basic"))))

    # Для товара с разными размерами отображаем минимальную доступную цену.
    if prices:
        sale_price, original_price = min(prices, key=lambda item: item[0])
    else:
        # Резерв для старой/упрощённой структуры ответа.
        sale_price = _rubles(product.get("salePriceU"))
        original_price = _rubles(product.get("priceU"))

    if not sale_price:
        return _wb_error(article, "У товара нет доступной цены")

    return {
        "article": article,
        "name": name,
        "price": sale_price,
        "card_price": 0,
        "original_price": original_price if original_price != sale_price else 0,
        "success": True,
        "error": "",
    }


def _request_wb_batch(articles: list[str]) -> dict[str, dict]:
    """
    Выполняет один пакетный запрос WB. Артикулы передаются через ";".
    """
    max_attempts = 3
    last_error = "Не удалось получить данные от WB API"

    for attempt in range(max_attempts):
        try:
            logger.info(
                f"WB: запрос {len(articles)} товаров "
                f"(попытка {attempt + 1}/{max_attempts})"
            )
            response = WB_SESSION.get(
                WB_API_URL,
                params={
                    "appType": 1,
                    "curr": "rub",
                    "dest": WB_DEST,
                    "spp": 30,
                    "locale": "ru",
                    "nm": ";".join(articles),
                },
                timeout=20,
            )

            if response.status_code == 429:
                delay = 5 * (2 ** attempt)
                logger.warning(f"WB ограничил запросы (HTTP 429). Пауза {delay} с.")
                last_error = "WB временно ограничил частоту запросов (HTTP 429)"
                time.sleep(delay)
                continue

            if response.status_code in (403, 498):
                last_error = f"WB отклонил запрос (HTTP {response.status_code})"
                logger.warning(last_error)
                break

            if response.status_code != 200:
                last_error = f"WB вернул HTTP {response.status_code}"
                logger.warning(last_error)
                if response.status_code >= 500:
                    time.sleep(3 * (attempt + 1))
                    continue
                break

            try:
                data = response.json()
            except ValueError:
                last_error = "WB вернул ответ не в формате JSON"
                logger.warning(last_error)
                continue

            # v4 обычно возвращает products в корне. Поддержка data.products
            # оставлена, чтобы код не ломался на альтернативном ответе WB.
            products = data.get("products") or (data.get("data") or {}).get("products") or []
            results = {}
            for product in products:
                parsed = parse_wb_product_data(product)
                if parsed["article"]:
                    results[parsed["article"]] = parsed

            for article in articles:
                results.setdefault(
                    article,
                    _wb_error(article, "Товар не найден или снят с продажи"),
                )
            return results

        except requests.RequestException as e:
            last_error = f"Ошибка соединения с WB: {e}"
            logger.warning(f"{last_error} (попытка {attempt + 1})")
            time.sleep(3 * (attempt + 1))
        except (TypeError, ValueError) as e:
            last_error = f"Неожиданная структура ответа WB: {e}"
            logger.warning(last_error)
            break

    return {article: _wb_error(article, last_error) for article in articles}


def get_prices_batch_wb(articles: list[str]) -> dict[str, dict]:
    """Получает все товары WB несколькими компактными пакетами."""
    clean_articles = list(dict.fromkeys(str(a).strip() for a in articles if str(a).strip()))
    results = {}

    for start in range(0, len(clean_articles), WB_BATCH_SIZE):
        batch = clean_articles[start:start + WB_BATCH_SIZE]
        results.update(_request_wb_batch(batch))
        if start + WB_BATCH_SIZE < len(clean_articles):
            time.sleep(2)

    return results


def get_price_by_article_wb(article: str) -> dict:
    """Совместимая функция-обертка для единичного запроса."""
    article = str(article).strip()
    return get_prices_batch_wb([article]).get(
        article, _wb_error(article, "Ошибка получения данных")
    )

CHECK_INTERVAL_SECONDS = 120  # 2 минуты
ARTICLES_FILE_OZON_DEFAULT = "articles_ozon.txt"
ARTICLES_FILE_WB_DEFAULT = "articles_wb.txt"
PRICE_STATE_FILE_DEFAULT = "price_state.json"
TELEGRAM_CONFIG_FILE_DEFAULT = "telegram_config.txt"

OZON_PRODUCT_URL_TEMPLATE = "https://www.ozon.ru/product/{article}/"
WB_PRODUCT_URL_TEMPLATE = "https://www.wildberries.ru/catalog/{article}/detail.aspx"

MARKETPLACES = {
    "ozon": {
        "label": "Ozon",
        "articles_file": ARTICLES_FILE_OZON_DEFAULT,
        "product_url_template": OZON_PRODUCT_URL_TEMPLATE,
        "fetch_price": get_price_by_article,
        "menu_button": "📦 Ozon",
    },
    "wb": {
        "label": "Wildberries",
        "articles_file": ARTICLES_FILE_WB_DEFAULT,
        "product_url_template": WB_PRODUCT_URL_TEMPLATE,
        "fetch_price": get_price_by_article_wb,
        "menu_button": "📦 Wildberries",
    },
}


def migrate_legacy_articles_file(old_path: str = "articles.txt", new_path: str = ARTICLES_FILE_OZON_DEFAULT) -> None:
    old = Path(old_path)
    new = Path(new_path)
    if old.exists() and not new.exists():
        old.rename(new)
        logger.info(f"Найден старый {old_path} — переименован в {new_path}.")


PRICE_FIELDS = [
    ("price", "Цена"),
    ("card_price", "Цена по карте"),
    ("original_price", "Старая цена"),
]

articles_lock = threading.Lock()
monitoring_enabled = threading.Event()
monitoring_enabled.set()

PAGE_SIZE = 8

monitor_status_lock = threading.Lock()
monitor_status: Dict = {
    "cycle_count": 0,
    "total_in_cycle": None,
    "last_cycle_finished_at": None,
    "next_check_at": None,
}


def load_articles(filepath: str) -> list:
    path = Path(filepath)

    if not path.exists():
        path.write_text(
            "# Список артикулов для мониторинга — по одному на строку.\n",
            encoding="utf-8",
        )
        return []

    articles = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        article = extract_article_from_input(line)
        if article.isdigit():
            articles.append(article)
        else:
            logger.warning(f"Пропущена некорректная строка в {filepath}: '{raw_line}'")

    return articles


def add_article_to_file(filepath: str, raw_value: str) -> tuple:
    article = extract_article_from_input(raw_value.strip())
    if not article.isdigit():
        return False, f"❌ Не удалось распознать артикул в «{raw_value}». Пришли число или ссылку на товар."

    existing = load_articles(filepath)
    if article in existing:
        return False, f"ℹ️ Артикул {article} уже отслеживается."

    with open(filepath, "a", encoding="utf-8") as f:
        f.write(f"{article}\n")

    return True, f"✅ Артикул {article} добавлен в отслеживание."


def remove_article_from_file(filepath: str, raw_value: str) -> tuple:
    article = extract_article_from_input(raw_value.strip())
    if not article.isdigit():
        return False, f"❌ Не удалось распознать артикул в «{raw_value}»."

    path = Path(filepath)
    if not path.exists():
        return False, "📋 Список артикулов пуст."

    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines = []
    removed = False

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            if extract_article_from_input(stripped) == article:
                removed = True
                continue
        new_lines.append(line)

    if not removed:
        return False, f"ℹ️ Артикул {article} не найден в списке отслеживания."

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True, f"🗑 Артикул {article} убран из отслеживания."


def build_main_menu_keyboard() -> ReplyKeyboardMarkup:
    marketplace_row = [KeyboardButton(text=mp["menu_button"]) for mp in MARKETPLACES.values()]
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Запустить"), KeyboardButton(text="⏸ Остановить")],
            marketplace_row,
        ],
        resize_keyboard=True,
    )


def build_articles_page(
    mp_key: str, marketplace_label: str, articles: List[str], page: int,
    price_state: Optional[Dict[str, Dict]] = None,
) -> tuple:
    price_state = price_state or {}

    total_pages = max(1, (len(articles) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    page_articles = articles[start:start + PAGE_SIZE]

    if articles:
        lines = [f"📦 {marketplace_label} (стр. {page + 1}/{total_pages}, всего {len(articles)})\n"]
        for i, article in enumerate(page_articles, start=1):
            name = (price_state.get(f"{mp_key}:{article}") or {}).get("name")
            if name:
                lines.append(f"{i}. {name} — {article}")
            else:
                lines.append(f"{i}. {article} (название появится после первой проверки)")
        text = "\n".join(lines)
    else:
        text = f"📦 {marketplace_label}: список пуст. Нажми «➕ Добавить», чтобы начать отслеживание."

    delete_buttons = [
        InlineKeyboardButton(text=f"❌ {i}", callback_data=f"del:{mp_key}:{article}:{page}")
        for i, article in enumerate(page_articles, start=1)
    ]
    rows = [delete_buttons[i:i + 4] for i in range(0, len(delete_buttons), 4)]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{mp_key}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text="➕ Добавить", callback_data=f"add:{mp_key}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{mp_key}:{page + 1}"))
    rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])

    return text, InlineKeyboardMarkup(inline_keyboard=rows), page


class ArticleStates(StatesGroup):
    waiting_for_article = State()


def get_status_text() -> str:
    with monitor_status_lock:
        status = dict(monitor_status)

    if status["cycle_count"] == 0:
        return "⏳ Мониторинг ещё не завершил ни одного круга — первые результаты появятся совсем скоро."

    next_check_str = (
        time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(status["next_check_at"]))
        if status["next_check_at"] else "неизвестно"
    )

    return (
        "📊 Статус мониторинга:\n"
        f"Кругов пройдено: {status['cycle_count']}\n"
        f"Товаров в последнем круге: {status['total_in_cycle']}\n"
        f"Следующая проверка: {next_check_str}"
    )


def load_price_state(filepath: str) -> Dict[str, Dict]:
    path = Path(filepath)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Не удалось прочитать файл состояния {filepath}: {e}.")
        return {}


def save_price_state(filepath: str, state: Dict[str, Dict]) -> None:
    try:
        Path(filepath).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"Не удалось сохранить файл состояния {filepath}: {e}")


def load_telegram_config(filepath: str) -> Optional[Dict[str, object]]:
    path = Path(filepath)

    if not path.exists():
        path.write_text(
            "BOT_TOKEN=\n"
            "CHAT_ID=\n",
            encoding="utf-8",
        )
        return None

    config: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        config[key.strip().upper()] = value.strip()

    bot_token = config.get("BOT_TOKEN", "")
    chat_id_raw = config.get("CHAT_ID", "")

    if not bot_token or not chat_id_raw:
        return None

    chat_ids = [cid.strip() for cid in chat_id_raw.split(",") if cid.strip()]
    if not chat_ids:
        return None

    return {"bot_token": bot_token, "chat_ids": chat_ids}


def verify_telegram_config(telegram_config: Dict[str, object]) -> bool:
    bot_token = telegram_config["bot_token"]
    chat_ids = telegram_config["chat_ids"]

    print("🔍 Проверяю настройки Telegram...")

    try:
        response = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=15)
    except Exception as e:
        print(f"❌ Не удалось связаться с Telegram API: {e}")
        return False

    if response.status_code != 200:
        print(f"❌ Telegram вернул ошибку при проверке токена: {response.status_code}")
        return False

    bot_info = response.json().get("result", {})
    print(f"✅ Токен верный. Бот: @{bot_info.get('username', 'неизвестно')}")

    test_message = "✅ Проверка связи. Доступ к боту мониторинга цен подтвержден."
    send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    verified_ids = []
    for chat_id in chat_ids:
        try:
            send_response = requests.post(send_url, data={"chat_id": chat_id, "text": test_message}, timeout=15)
            if send_response.status_code == 200:
                verified_ids.append(chat_id)
        except Exception:
            continue

    if not verified_ids:
        print("❌ Ни один CHAT_ID не прошёл проверку.")
        return False

    telegram_config["chat_ids"] = verified_ids
    return True


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        response = requests.post(url, data=payload, timeout=15)
        return response.status_code == 200
    except Exception as e:
        logger.warning(f"Не удалось отправить сообщение в Telegram: {e}")
        return False


def build_info_lines(data: Dict) -> List[str]:
    lines = []
    for field_key, field_label in PRICE_FIELDS:
        value = data.get(field_key)
        if value:
            lines.append(f"{field_label}: {value} ₽")
    return lines


def build_price_drop_message(
    marketplace_label: str, product_url_template: str,
    article: str, name: str, field_label: str,
    old_value: int, new_value: int,
    previous_data: Dict, current_data: Dict,
) -> str:
    decrease = old_value - new_value
    percent = (decrease / old_value * 100) if old_value else 0
    link = product_url_template.format(article=article)

    lines = [
        f"[{marketplace_label}] {name}",
        f"{field_label} снизилась: {decrease} ₽ ({percent:.1f}%)",
        link,
        "",
        "Было:",
        *build_info_lines(previous_data),
        "",
        "Стало:",
        *build_info_lines(current_data),
    ]
    return "\n".join(lines)


def process_and_print(
    mp_key: str, marketplace_label: str, product_url_template: str,
    result: Dict, previous_data: Optional[Dict], telegram_config: Optional[Dict[str, object]],
) -> Optional[Dict]:
    timestamp = time.strftime("%d.%m.%Y %H:%M:%S")
    print(f"\n[{timestamp}] [{marketplace_label}]")

    if not result["success"]:
        print(f"❌ Артикул {result['article']}: не удалось получить цену — {result['error']}")
        return None

    print(f"✅ Товар: {result['name']}")
    print(f"   Артикул: {result['article']}")

    current_data = {
        "name": result["name"],
        "price": result["price"],
        "card_price": result["card_price"],
        "original_price": result["original_price"],
        "last_checked": timestamp,
    }

    for field_key, field_label in PRICE_FIELDS:
        current_value = result[field_key]
        if not current_value:
            continue

        previous_value = previous_data.get(field_key) if previous_data else None
        line = f"   {field_label}: {current_value} ₽"

        if previous_value and current_value < previous_value:
            decrease = previous_value - current_value
            line = (
                f"{COLOR_GREEN}   {field_label}: {current_value} ₽ "
                f"(было {previous_value} ₽, снижение на {decrease} ₽) 📉{COLOR_RESET}"
            )

            message = build_price_drop_message(
                marketplace_label, product_url_template,
                result["article"], result["name"], field_label,
                previous_value, current_value, previous_data, current_data,
            )

            if telegram_config and telegram_config.get("chat_ids"):
                chat_ids = telegram_config["chat_ids"]
                sum(
                    1 for chat_id in chat_ids
                    if send_telegram_message(telegram_config["bot_token"], chat_id, message)
                )

        elif previous_value and current_value > previous_value:
            increase = current_value - previous_value
            line += f"  (было {previous_value} ₽, рост на {increase} ₽) 📈"

        print(line)

    return current_data


def monitor_articles(
    telegram_config: Optional[Dict[str, object]],
    interval_seconds: int = CHECK_INTERVAL_SECONDS,
    state_filepath: str = PRICE_STATE_FILE_DEFAULT,
) -> None:
    price_state = load_price_state(state_filepath)

    print("🔁 Мониторинг запущен.")

    while True:
        monitoring_enabled.wait()

        # Последовательность намеренно фиксирована: сначала весь Ozon,
        # затем весь Wildberries.
        with articles_lock:
            ozon_articles = load_articles(MARKETPLACES["ozon"]["articles_file"])
            wb_articles = load_articles(MARKETPLACES["wb"]["articles_file"])

        total_items = len(ozon_articles) + len(wb_articles)
        if not total_items:
            print("⚠️ Списки артикулов пусты.")
        else:
            with monitor_status_lock:
                monitor_status["total_in_cycle"] = total_items

            def handle_result(mp_key: str, article: str, result: Dict) -> None:
                mp = MARKETPLACES[mp_key]
                state_key = f"{mp_key}:{article}"
                try:
                    previous_data = price_state.get(state_key)
                    current_data = process_and_print(
                        mp_key, mp["label"], mp["product_url_template"],
                        result, previous_data, telegram_config,
                    )

                    if current_data is not None:
                        price_state[state_key] = current_data
                        save_price_state(state_filepath, price_state)
                except Exception as e:
                    logger.error(f"Неожиданная ошибка при проверке {mp['label']}:{article}: {e}")

            # Ozon требует отдельного браузерного прохода для каждого артикула.
            for article in ozon_articles:
                if not monitoring_enabled.is_set():
                    break
                handle_result("ozon", article, get_price_by_article(article))
                if article != ozon_articles[-1] or wb_articles:
                    time.sleep(3)

            # WB получает весь список пакетно, а затем результаты разбираются в
            # том же порядке, в котором артикулы записаны в articles_wb.txt.
            if monitoring_enabled.is_set() and wb_articles:
                wb_results = get_prices_batch_wb(wb_articles)
                for article in wb_articles:
                    if not monitoring_enabled.is_set():
                        break
                    handle_result(
                        "wb",
                        article,
                        wb_results.get(article, _wb_error(article, "WB не вернул товар")),
                    )

            with monitor_status_lock:
                monitor_status["cycle_count"] += 1
                monitor_status["last_cycle_finished_at"] = time.time()
                monitor_status["next_check_at"] = time.time() + interval_seconds

        try:
            sleep_remaining = interval_seconds
            while sleep_remaining > 0:
                if not monitoring_enabled.is_set():
                    break
                chunk = min(1, sleep_remaining)
                time.sleep(chunk)
                sleep_remaining -= chunk
        except KeyboardInterrupt:
            break


async def run_telegram_bot(telegram_config: Dict[str, object]) -> None:
    bot = Bot(token=telegram_config["bot_token"])
    dp = Dispatcher(storage=MemoryStorage())
    allowed_chat_ids = set(str(cid) for cid in telegram_config["chat_ids"])

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    def is_authorized(event) -> bool:
        chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
        return str(chat_id) in allowed_chat_ids

    async def render_articles_page(chat_id: int, message_id: Optional[int], mp_key: str, page: int) -> int:
        mp = MARKETPLACES[mp_key]
        with articles_lock:
            articles = load_articles(mp["articles_file"])
        price_state = load_price_state(PRICE_STATE_FILE_DEFAULT)
        text, keyboard, _ = build_articles_page(mp_key, mp["label"], articles, page, price_state)

        if message_id:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=keyboard)
            return message_id

        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        return sent.message_id

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        if not is_authorized(message):
            return
        await message.answer(
            "👋 Бот мониторинга цен запущен.",
            reply_markup=build_main_menu_keyboard(),
        )

    @dp.message(F.text == "▶️ Запустить")
    async def btn_start_monitoring(message: Message):
        if not is_authorized(message):
            return
        monitoring_enabled.set()
        await message.answer("▶️ Мониторинг запущен.")

    @dp.message(F.text == "⏸ Остановить")
    async def btn_stop_monitoring(message: Message):
        if not is_authorized(message):
            return
        monitoring_enabled.clear()
        await message.answer("⏸ Мониторинг остановлен.")

    def make_manage_handler(mp_key: str):
        async def handler(message: Message):
            if not is_authorized(message):
                return
            await render_articles_page(message.chat.id, None, mp_key, page=0)
        return handler

    for _mp_key, _mp in MARKETPLACES.items():
        dp.message(F.text == _mp["menu_button"])(make_manage_handler(_mp_key))

    @dp.callback_query(F.data.startswith("del:"))
    async def cb_delete_article(callback: CallbackQuery):
        if not is_authorized(callback):
            return
        _, mp_key, article, page_str = callback.data.split(":")
        mp = MARKETPLACES[mp_key]
        with articles_lock:
            _, reply_text = remove_article_from_file(mp["articles_file"], article)
        await render_articles_page(callback.message.chat.id, callback.message.message_id, mp_key, page=int(page_str))
        await callback.answer(reply_text)

    @dp.callback_query(F.data.startswith("page:"))
    async def cb_change_page(callback: CallbackQuery):
        if not is_authorized(callback):
            return
        _, mp_key, page_str = callback.data.split(":")
        await render_articles_page(callback.message.chat.id, callback.message.message_id, mp_key, page=int(page_str))
        await callback.answer()

    @dp.callback_query(F.data.startswith("add:"))
    async def cb_add_article(callback: CallbackQuery, state: FSMContext):
        if not is_authorized(callback):
            return
        mp_key = callback.data.split(":")[1]
        mp = MARKETPLACES[mp_key]
        await state.set_state(ArticleStates.waiting_for_article)
        await state.update_data(
            list_chat_id=callback.message.chat.id,
            list_message_id=callback.message.message_id,
            mp_key=mp_key,
        )
        await callback.message.edit_text(
            f"✏️ Пришли артикул или ссылку на товар {mp['label']} одним сообщением."
        )
        await callback.answer()

    @dp.callback_query(F.data == "back")
    async def cb_back(callback: CallbackQuery):
        if not is_authorized(callback):
            return
        await callback.message.edit_text("↩️ Возврат в меню.")
        await callback.answer()

    @dp.message(StateFilter(ArticleStates.waiting_for_article))
    async def handle_new_article_input(message: Message, state: FSMContext):
        if not is_authorized(message):
            return

        menu_buttons = {mp["menu_button"] for mp in MARKETPLACES.values()}
        if message.text in menu_buttons | {"▶️ Запустить", "⏸ Остановить"}:
            await state.clear()
            if message.text in menu_buttons:
                mp_key = next(k for k, mp in MARKETPLACES.items() if mp["menu_button"] == message.text)
                await render_articles_page(message.chat.id, None, mp_key, page=0)
            return

        data = await state.get_data()
        mp_key = data["mp_key"]
        mp = MARKETPLACES[mp_key]
        with articles_lock:
            _, reply_text = add_article_to_file(mp["articles_file"], message.text)
        await state.clear()
        await message.answer(reply_text)
        await render_articles_page(data["list_chat_id"], data["list_message_id"], mp_key, page=0)

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        if not is_authorized(message):
            return
        await message.answer(get_status_text())

    await dp.start_polling(bot)


def main():
    enable_ansi_colors()
    migrate_legacy_articles_file()

    telegram_config = load_telegram_config(TELEGRAM_CONFIG_FILE_DEFAULT)
    if telegram_config and not verify_telegram_config(telegram_config):
        telegram_config = None

    monitor_thread = threading.Thread(
        target=monitor_articles,
        args=(telegram_config,),
        daemon=True,
    )
    monitor_thread.start()

    if telegram_config:
        try:
            asyncio.run(run_telegram_bot(telegram_config))
        except KeyboardInterrupt:
            pass
    else:
        try:
            monitor_thread.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()