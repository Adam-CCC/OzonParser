#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ozon Price Checker
------------------
Утилита: мониторит цены товаров Ozon по списку артикулов и при снижении цены
отправляет уведомление в Telegram. Управление — через кнопки бота.

Использование:
    python ozon_price.py
    python ozon_price.py my_articles.txt

Требования:
    pip install selenium selenium-stealth requests aiogram
    Установленный Google Chrome (версия должна совпадать с chromedriver,
    Selenium 4.15+ обычно подтягивает драйвер автоматически).

Файлы, которые программа создаёт и ведёт сама:
    articles.txt          — список отслеживаемых артикулов (можно править и вручную)
    price_state.json       — данные предыдущего цикла по каждому артикулу
    telegram_config.txt    — токен бота и chat_id (нужно заполнить один раз)

Интерфейс бота (доступен только из чата, указанного в telegram_config.txt):
    ▶️ Запустить              — включить фоновый мониторинг цен
    ⏸ Остановить              — приостановить мониторинг (список сохраняется)
    📦 Управление артикулами  — список товаров с кнопками ❌ удалить / ➕ добавить
    /status                   — статус мониторинга текстом
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
            pass  # если не получилось — просто останемся без цвета, на работу это не влияет


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
    Главная функция: возвращает словарь с ценой товара по артикулу.

    Возвращает:
        {
            'article': str,
            'name': str,
            'price': int,           # текущая цена
            'card_price': int,      # цена по карте Ozon
            'original_price': int,  # старая (зачёркнутая) цена
            'success': bool,
            'error': str,
        }
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
    match = re.search(r"/product/[^/]+-(\d+)/?", raw)
    if match:
        return match.group(1)
    return raw


CHECK_INTERVAL_SECONDS = 120  # 2 минуты
ARTICLES_FILE_DEFAULT = "articles.txt"
PRICE_STATE_FILE_DEFAULT = "price_state.json"          # данные предыдущего цикла по каждому артикулу
TELEGRAM_CONFIG_FILE_DEFAULT = "telegram_config.txt"    # токен бота и chat_id

PRODUCT_URL_TEMPLATE = "https://www.ozon.ru/product/{article}/"

# Какие поля сравниваем между циклами и как подписываем их в консоли/сообщении
PRICE_FIELDS = [
    ("price", "Цена"),
    ("card_price", "Цена по карте"),
    ("original_price", "Старая цена"),
]

# articles.txt читает и пишет и фоновый цикл мониторинга, и Telegram-бот (из другого потока) —
# блокировка нужна, чтобы не столкнуться с одновременной записью/чтением файла
articles_lock = threading.Lock()

# Переключатель "Запустить/Остановить" — управляется кнопками бота, проверяется циклом мониторинга.
# По умолчанию мониторинг работает сразу после старта программы.
monitoring_enabled = threading.Event()
monitoring_enabled.set()

PAGE_SIZE = 8  # сколько артикулов показывать на одной "странице" в разделе управления

# Общий статус, который читает команда /status — обновляется мониторинг-циклом
monitor_status_lock = threading.Lock()
monitor_status: Dict = {
    "cycle_count": 0,
    "total_in_cycle": None,
    "last_cycle_finished_at": None,
    "next_check_at": None,
}


def load_articles(filepath: str) -> list:
    """
    Читает список артикулов из текстового файла — по одному на строку.
    Пустые строки и строки, начинающиеся с '#', пропускаются.
    Допускаются как чистые артикулы, так и полные ссылки на товар.
    """
    path = Path(filepath)

    if not path.exists():
        path.write_text(
            "# Список артикулов Ozon для мониторинга — по одному на строку.\n"
            "# Строки, начинающиеся с '#', игнорируются.\n"
            "# Можно вставлять как чистый артикул, так и полную ссылку на товар.\n"
            "#\n"
            "# Пример:\n"
            "# 123456789\n"
            "# https://www.ozon.ru/product/nazvanie-tovara-987654321/\n",
            encoding="utf-8",
        )
        logger.warning(f"Файл {filepath} не найден — создан пустой шаблон. Заполни его артикулами и перезапусти программу.")
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
    """
    Добавляет артикул в файл списка (используется командой бота /add).
    Возвращает (успех: bool, текст ответа пользователю: str).
    """
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
    """
    Убирает артикул из файла списка (используется командой бота /remove).
    Возвращает (успех: bool, текст ответа пользователю: str).
    """
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


def list_articles_text(filepath: str) -> str:
    """Формирует текст со списком отслеживаемых артикулов для команды бота /list."""
    articles = load_articles(filepath)
    if not articles:
        return "📋 Список артикулов пуст. Добавь товар командой /add <артикул или ссылка>."

    lines = [f"📋 Отслеживается артикулов: {len(articles)}\n"]
    for i, article in enumerate(articles, start=1):
        lines.append(f"{i}. {article} — {PRODUCT_URL_TEMPLATE.format(article=article)}")
    return "\n".join(lines)


def build_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянное меню внизу экрана: Запустить / Остановить / Управление артикулами."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Запустить"), KeyboardButton(text="⏸ Остановить")],
            [KeyboardButton(text="📦 Управление артикулами")],
        ],
        resize_keyboard=True,
    )


def build_articles_page(articles: List[str], page: int, price_state: Optional[Dict[str, Dict]] = None) -> tuple:
    """
    Строит текст и inline-клавиатуру для одной "страницы" списка артикулов.
    Полные названия товаров (без обрезки) выводятся нумерованным списком в тексте
    сообщения — там нет ограничений ширины экрана, в отличие от кнопок. Сами кнопки
    удаления компактные — просто номер строки, чтобы не разъезжаться на телефоне.
    Возвращает (текст, клавиатура, номер_фактической_страницы).
    """
    price_state = price_state or {}

    total_pages = max(1, (len(articles) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    page_articles = articles[start:start + PAGE_SIZE]

    if articles:
        lines = [f"📦 Управление артикулами (стр. {page + 1}/{total_pages}, всего {len(articles)})\n"]
        for i, article in enumerate(page_articles, start=1):
            name = (price_state.get(article) or {}).get("name")
            if name:
                lines.append(f"{i}. {name} — {article}")
            else:
                lines.append(f"{i}. {article} (название появится после первой проверки)")
        text = "\n".join(lines)
    else:
        text = "📦 Список артикулов пуст. Нажми «➕ Добавить», чтобы начать отслеживание."

    # Кнопки удаления собираем в один ряд по несколько штук, чтобы список не растягивался по вертикали
    delete_buttons = [
        InlineKeyboardButton(text=f"❌ {i}", callback_data=f"del:{article}:{page}")
        for i, article in enumerate(page_articles, start=1)
    ]
    rows = [delete_buttons[i:i + 4] for i in range(0, len(delete_buttons), 4)]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text="➕ Добавить", callback_data="add"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{page + 1}"))
    rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])

    return text, InlineKeyboardMarkup(inline_keyboard=rows), page


class ArticleStates(StatesGroup):
    """Состояния диалога для сценария 'жду ввод нового артикула'."""
    waiting_for_article = State()


def get_status_text() -> str:
    """Формирует текст статуса мониторинга для команды бота /status."""
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
    """Загружает сохранённые данные о ценах с предыдущего цикла (переживает даже перезапуск программы)."""
    path = Path(filepath)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Не удалось прочитать файл состояния {filepath}: {e}. Начинаю с чистого состояния.")
        return {}


def save_price_state(filepath: str, state: Dict[str, Dict]) -> None:
    """Сохраняет текущие данные о ценах, чтобы сравнивать с ними на следующем цикле."""
    try:
        Path(filepath).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"Не удалось сохранить файл состояния {filepath}: {e}")


def verify_telegram_config(telegram_config: Dict[str, str]) -> bool:
    """
    Проверяет, что BOT_TOKEN и CHAT_ID введены верно:
    1) спрашивает у Telegram данные о боте (getMe) — так проверяется токен;
    2) отправляет тестовое сообщение на CHAT_ID — так проверяется id чата.
    Печатает понятный результат проверки в консоль. Возвращает True, если всё в порядке.
    """
    bot_token = telegram_config["bot_token"]
    chat_id = telegram_config["chat_id"]

    print("🔍 Проверяю настройки Telegram...")

    # Шаг 1: проверка токена бота
    try:
        response = requests.get(f"https://api.telegram.org/bot{bot_token}/getMe", timeout=15)
    except Exception as e:
        print(f"❌ Не удалось связаться с Telegram API: {e}")
        return False

    if response.status_code == 401:
        print("❌ BOT_TOKEN неверный — Telegram отвечает 'Unauthorized'. Проверь токен, скопированный от @BotFather.")
        return False
    if response.status_code != 200:
        print(f"❌ Telegram вернул ошибку при проверке токена: {response.status_code} {response.text}")
        return False

    bot_info = response.json().get("result", {})
    bot_username = bot_info.get("username", "неизвестно")
    print(f"✅ Токен верный. Бот: @{bot_username}")

    # Шаг 2: проверка chat_id — реальной отправкой тестового сообщения
    test_message = (
        "✅ Проверка связи.\n"
        "Если ты видишь это сообщение — BOT_TOKEN и CHAT_ID указаны верно, "
        "мониторинг цен Ozon запущен и уведомления будут приходить сюда."
    )
    send_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        send_response = requests.post(send_url, data={"chat_id": chat_id, "text": test_message}, timeout=15)
    except Exception as e:
        print(f"❌ Не удалось отправить тестовое сообщение: {e}")
        return False

    if send_response.status_code == 200:
        print(f"✅ CHAT_ID верный. Тестовое сообщение отправлено — проверь Telegram.")
        return True

    error_description = send_response.json().get("description", send_response.text)
    print(f"❌ CHAT_ID неверный или бот не может писать в этот чат: {error_description}")
    print("   Убедись, что ты сначала написал боту любое сообщение (например 'привет'),")
    print("   и что CHAT_ID скопирован правильно (обычно это просто число, у групп — со знаком минус).")
    return False


def load_telegram_config(filepath: str) -> Optional[Dict[str, str]]:
    """
    Читает токен бота и chat_id из простого текстового файла формата KEY=VALUE.
    Если файла нет — создаёт шаблон с инструкцией и возвращает None
    (уведомления в Telegram в этом случае просто не отправляются).
    """
    path = Path(filepath)

    if not path.exists():
        path.write_text(
            "# Настройки Telegram-уведомлений о снижении цены.\n"
            "# 1. Создай бота через @BotFather в Telegram, получи токен вида 123456:ABC-DEF...\n"
            "# 2. Напиши своему боту любое сообщение (просто 'привет'), чтобы он тебя увидел.\n"
            "# 3. Узнай свой chat_id — например, через бота @userinfobot (он пришлёт его в ответ на /start).\n"
            "# 4. Впиши оба значения ниже без кавычек и перезапусти программу.\n"
            "#\n"
            "BOT_TOKEN=\n"
            "CHAT_ID=\n",
            encoding="utf-8",
        )
        logger.warning(
            f"Файл {filepath} не найден — создан шаблон. Заполни BOT_TOKEN и CHAT_ID, "
            f"иначе уведомления о снижении цены отправляться не будут."
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
    chat_id = config.get("CHAT_ID", "")

    if not bot_token or not chat_id:
        logger.warning(
            f"В файле {filepath} не заполнены BOT_TOKEN и/или CHAT_ID — "
            f"уведомления о снижении цены отправляться не будут."
        )
        return None

    return {"bot_token": bot_token, "chat_id": chat_id}


def send_telegram_message(telegram_config: Dict[str, str], text: str) -> bool:
    """Отправляет текстовое сообщение в Telegram через Bot API. Возвращает True при успехе."""
    url = f"https://api.telegram.org/bot{telegram_config['bot_token']}/sendMessage"
    payload = {"chat_id": telegram_config["chat_id"], "text": text}

    try:
        response = requests.post(url, data=payload, timeout=15)
        if response.status_code == 200:
            return True
        logger.warning(f"Telegram вернул ошибку {response.status_code}: {response.text}")
        return False
    except Exception as e:
        logger.warning(f"Не удалось отправить сообщение в Telegram: {e}")
        return False


def build_info_lines(data: Dict) -> List[str]:
    """Строит список строк 'Показатель: значение ₽' для всех заполненных ценовых полей."""
    lines = []
    for field_key, field_label in PRICE_FIELDS:
        value = data.get(field_key)
        if value:
            lines.append(f"{field_label}: {value} ₽")
    return lines


def build_price_drop_message(
    article: str, name: str, field_label: str,
    old_value: int, new_value: int,
    previous_data: Dict, current_data: Dict,
) -> str:
    """Формирует текст уведомления о снижении цены в запрошенном формате."""
    decrease = old_value - new_value
    percent = (decrease / old_value * 100) if old_value else 0
    link = PRODUCT_URL_TEMPLATE.format(article=article)

    lines = [
        name,
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
    result: Dict, previous_data: Optional[Dict], telegram_config: Optional[Dict[str, str]],
) -> Optional[Dict]:
    """
    Печатает результат проверки одного артикула, сравнивая его с данными предыдущего цикла.
    Если по какому-то из показателей (цена / цена по карте / старая цена) произошло
    снижение — строка подсвечивается ярко-зелёным и в Telegram отправляется уведомление
    с полным набором данных "было / стало".

    Возвращает данные для сохранения в состояние (None, если проверка не удалась).
    """
    timestamp = time.strftime("%d.%m.%Y %H:%M:%S")
    print(f"\n[{timestamp}]")

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

    any_decrease = False

    for field_key, field_label in PRICE_FIELDS:
        current_value = result[field_key]
        if not current_value:
            continue  # у товара может не быть, например, цены по карте

        previous_value = previous_data.get(field_key) if previous_data else None
        line = f"   {field_label}: {current_value} ₽"

        if previous_value and current_value < previous_value:
            decrease = previous_value - current_value
            line = (
                f"{COLOR_GREEN}   {field_label}: {current_value} ₽ "
                f"(было {previous_value} ₽, снижение на {decrease} ₽) 📉{COLOR_RESET}"
            )
            any_decrease = True

            message = build_price_drop_message(
                result["article"], result["name"], field_label,
                previous_value, current_value, previous_data, current_data,
            )

            if telegram_config:
                sent = send_telegram_message(telegram_config, message)
                if sent:
                    print(f"{COLOR_GREEN}   🟢 Уведомление о снижении цены отправлено в Telegram{COLOR_RESET}")
                else:
                    print(f"{COLOR_GREEN}   🟢 Снижение цены зафиксировано, но отправить в Telegram не удалось{COLOR_RESET}")
            else:
                print(f"{COLOR_GREEN}   🟢 Снижение цены зафиксировано (Telegram не настроен — см. telegram_config.txt){COLOR_RESET}")

        elif previous_value and current_value > previous_value:
            increase = current_value - previous_value
            line += f"  (было {previous_value} ₽, рост на {increase} ₽) 📈"

        print(line)

    return current_data


def monitor_articles(
    filepath: str,
    telegram_config: Optional[Dict[str, str]],
    interval_seconds: int = CHECK_INTERVAL_SECONDS,
    state_filepath: str = PRICE_STATE_FILE_DEFAULT,
) -> None:
    """
    Бесконечно обходит список артикулов из файла.
    Пауза interval_seconds делается один раз — после того, как пройден весь список
    (то есть после получения данных по последнему артикулу), а не после каждого артикула.
    Список перечитывается из файла в начале каждого круга — можно дописывать
    артикулы через Telegram-бота, не перезапуская программу.

    Данные предыдущего цикла (цена, цена по карте, старая цена) хранятся в state_filepath
    и переживают даже перезапуск программы. Если по какому-то артикулу цена снизилась —
    строка подсвечивается зелёным и уведомление отправляется в Telegram.
    """
    price_state = load_price_state(state_filepath)

    print(f"🔁 Мониторинг запущен. Файл со списком артикулов: {filepath}")
    print(f"   Пауза между кругами: {interval_seconds // 60} мин (отсчёт — после последнего артикула в списке).")
    print(f"   Файл состояния: {state_filepath}")
    print(f"   Telegram-уведомления: {'включены' if telegram_config else 'отключены'}")
    print("   Останови программу сочетанием Ctrl+C, когда будет нужно.\n")

    while True:
        monitoring_enabled.wait()  # если нажата "Остановить" — просто ждём тут, круг не начинается

        with articles_lock:
            articles = load_articles(filepath)

        if not articles:
            print(f"⚠️ Список артикулов пуст. Добавь артикулы через Telegram-бота (кнопка «📦 Управление артикулами») или в {filepath}.")
        else:
            print(f"📋 В этом круге будет проверено артикулов: {len(articles)}")

            with monitor_status_lock:
                monitor_status["total_in_cycle"] = len(articles)

            for index, article in enumerate(articles, start=1):
                if not monitoring_enabled.is_set():
                    print("⏸ Мониторинг остановлен кнопкой — прерываю текущий круг.")
                    break

                try:
                    result = get_price_by_article(article)
                    previous_data = price_state.get(article)
                    current_data = process_and_print(result, previous_data, telegram_config)

                    if current_data is not None:
                        price_state[article] = current_data
                        save_price_state(state_filepath, price_state)

                except Exception as e:
                    logger.error(f"Неожиданная ошибка при проверке артикула {article}: {e}")

                is_last_in_cycle = index == len(articles)
                if not is_last_in_cycle:
                    # Между артикулами внутри одного круга небольшая техническая пауза,
                    # чтобы не долбить сайт запросами впритык друг к другу
                    time.sleep(3)

            with monitor_status_lock:
                monitor_status["cycle_count"] += 1
                monitor_status["last_cycle_finished_at"] = time.time()
                monitor_status["next_check_at"] = time.time() + interval_seconds

        try:
            print(f"\n⏳ Круг завершён. Следующий круг через {interval_seconds // 60} мин...")
            sleep_remaining = interval_seconds
            while sleep_remaining > 0:
                if not monitoring_enabled.is_set():
                    print("⏸ Мониторинг остановлен кнопкой во время паузы между кругами.")
                    break
                chunk = min(1, sleep_remaining)
                time.sleep(chunk)
                sleep_remaining -= chunk
        except KeyboardInterrupt:
            print("\n🛑 Мониторинг остановлен пользователем.")
            break


async def run_telegram_bot(telegram_config: Dict[str, str], articles_filepath: str) -> None:
    """
    Запускает Telegram-бота с кнопочным интерфейсом:
    ▶️ Запустить / ⏸ Остановить — управляют фоновым циклом мониторинга (тот крутится в отдельном потоке).
    📦 Управление артикулами — открывает inline-меню со списком, кнопками удаления и добавления.
    Отвечает только пользователю из чата, указанного в telegram_config.txt.
    """
    bot = Bot(token=telegram_config["bot_token"])
    dp = Dispatcher(storage=MemoryStorage())
    allowed_chat_id = str(telegram_config["chat_id"])

    def is_authorized(event) -> bool:
        chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
        return str(chat_id) == allowed_chat_id

    async def render_articles_page(chat_id: int, message_id: Optional[int], page: int) -> int:
        """Отправляет (или обновляет, если message_id передан) страницу со списком артикулов. Возвращает id сообщения."""
        with articles_lock:
            articles = load_articles(articles_filepath)
        price_state = load_price_state(PRICE_STATE_FILE_DEFAULT)
        text, keyboard, _ = build_articles_page(articles, page, price_state)

        if message_id:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=keyboard)
            return message_id

        sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        return sent.message_id

    # --- Кнопки главного меню (обычная клавиатура снизу экрана) ---

    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        if not is_authorized(message):
            await message.answer("⛔ Этот бот настроен для другого пользователя.")
            return
        await message.answer(
            "👋 Бот мониторинга цен Ozon запущен.\nИспользуй кнопки внизу экрана.",
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
        await message.answer("⏸ Мониторинг остановлен. Список артикулов по-прежнему можно редактировать.")

    @dp.message(F.text == "📦 Управление артикулами")
    async def btn_manage_articles(message: Message):
        if not is_authorized(message):
            return
        await render_articles_page(message.chat.id, None, page=0)

    # --- Inline-кнопки внутри раздела "Управление артикулами" ---

    @dp.callback_query(F.data.startswith("del:"))
    async def cb_delete_article(callback: CallbackQuery):
        if not is_authorized(callback):
            await callback.answer()
            return
        _, article, page_str = callback.data.split(":")
        with articles_lock:
            _, reply_text = remove_article_from_file(articles_filepath, article)
        await render_articles_page(callback.message.chat.id, callback.message.message_id, page=int(page_str))
        await callback.answer(reply_text)

    @dp.callback_query(F.data.startswith("page:"))
    async def cb_change_page(callback: CallbackQuery):
        if not is_authorized(callback):
            await callback.answer()
            return
        page = int(callback.data.split(":")[1])
        await render_articles_page(callback.message.chat.id, callback.message.message_id, page=page)
        await callback.answer()

    @dp.callback_query(F.data == "add")
    async def cb_add_article(callback: CallbackQuery, state: FSMContext):
        if not is_authorized(callback):
            await callback.answer()
            return
        await state.set_state(ArticleStates.waiting_for_article)
        await state.update_data(list_chat_id=callback.message.chat.id, list_message_id=callback.message.message_id)
        await callback.message.edit_text(
            "✏️ Пришли артикул или ссылку на товар одним сообщением.\n"
            "Чтобы отменить — просто нажми «📦 Управление артикулами» ещё раз."
        )
        await callback.answer()

    @dp.callback_query(F.data == "back")
    async def cb_back(callback: CallbackQuery):
        if not is_authorized(callback):
            await callback.answer()
            return
        await callback.message.edit_text("↩️ Возврат в меню. Используй кнопки внизу экрана.")
        await callback.answer()

    # --- Ввод нового артикула текстом, когда бот его ждёт ---

    @dp.message(StateFilter(ArticleStates.waiting_for_article))
    async def handle_new_article_input(message: Message, state: FSMContext):
        if not is_authorized(message):
            return

        # Позволяем выйти из режима добавления, если человек снова нажал кнопку меню
        if message.text in ("📦 Управление артикулами", "▶️ Запустить", "⏸ Остановить"):
            await state.clear()
            if message.text == "📦 Управление артикулами":
                await render_articles_page(message.chat.id, None, page=0)
            return

        data = await state.get_data()
        with articles_lock:
            _, reply_text = add_article_to_file(articles_filepath, message.text)
        await state.clear()
        await message.answer(reply_text)
        await render_articles_page(data["list_chat_id"], data["list_message_id"], page=0)

    # --- Служебные текстовые команды остаются доступны как альтернатива кнопкам ---

    @dp.message(Command("list"))
    async def cmd_list(message: Message):
        if not is_authorized(message):
            return
        await render_articles_page(message.chat.id, None, page=0)

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        if not is_authorized(message):
            return
        await message.answer(get_status_text())

    logger.info("Telegram-бот запущен: кнопки ▶️/⏸/📦 плюс команды /list, /status")
    await dp.start_polling(bot)


def main():
    enable_ansi_colors()

    filepath = sys.argv[1] if len(sys.argv) > 1 else ARTICLES_FILE_DEFAULT

    telegram_config = load_telegram_config(TELEGRAM_CONFIG_FILE_DEFAULT)
    if telegram_config and not verify_telegram_config(telegram_config):
        print(
            f"⚠️ Проверка Telegram не пройдена — исправь {TELEGRAM_CONFIG_FILE_DEFAULT} и перезапусти программу.\n"
            f"   Мониторинг цен продолжится, но бот управления и уведомления работать не будут."
        )
        telegram_config = None

    # Фоновый мониторинг цен крутится в отдельном потоке независимо от Telegram-бота
    monitor_thread = threading.Thread(
        target=monitor_articles,
        args=(filepath, telegram_config),
        daemon=True,
    )
    monitor_thread.start()

    if telegram_config:
        try:
            asyncio.run(run_telegram_bot(telegram_config, filepath))
        except KeyboardInterrupt:
            print("\n🛑 Бот и мониторинг остановлены пользователем.")
    else:
        print("ℹ️ Telegram не настроен — бот управления не запущен, работает только консольный мониторинг цен.")
        try:
            monitor_thread.join()
        except KeyboardInterrupt:
            print("\n🛑 Мониторинг остановлен пользователем.")


if __name__ == "__main__":
    main()