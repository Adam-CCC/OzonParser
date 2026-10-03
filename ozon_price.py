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
    pip install selenium selenium-stealth requests aiogram seleniumbase
    Установленный Google Chrome (нужен только для Ozon; Selenium 4.15+
    обычно подтягивает драйвер автоматически).

    Wildberries проверяется без браузера — прямым запросом к внутреннему
    API сайта (/__internal/u-card/cards/v4/detail). Для него нужны cookie
    x_wbaas_token и заголовок deviceid. Токен берётся через seleniumbase
    (uc-режим, без окна) при запуске и при ответе 498/403 — как в проекте
    github.com/Duff89/wb_parse_search_phrase. Нужен: pip install seleniumbase

Файлы, которые программа создаёт и ведёт сама:
    articles_ozon.txt      — список отслеживаемых артикулов Ozon
    articles_wb.txt        — список отслеживаемых артикулов Wildberries
    price_state.json       — данные предыдущего цикла по каждому артикулу (обеих площадок)
    telegram_config.txt    — токен бота и chat_id (нужно заполнить один раз)
"""

import sys
import os
import json
import base64
import uuid
import re
import time
import random
import asyncio
import threading
import logging
from pathlib import Path
from typing import Optional, Dict, List

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
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

# seleniumbase (uc-режим) пишет в лог INFO-трейсбеки "KeyError: privateNetworkRequestPolicy",
# когда его пакет mycdp старше, чем Chrome. На работу это не влияет — просто глушим шум.
# (Лечится и обновлением: pip install -U mycdp)
for _noisy in ("uc.connection", "seleniumbase", "websockets", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

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


# Признаки того, что товар Ozon закончился (ищем в ключах и тексте виджетов ответа).
OZON_OUT_OF_STOCK_MARKERS = [
    "товар закончился", "нет в наличии", "закончился", "outofstock", "out_of_stock",
    "сообщить о поступлении", "нет в продаже",
]


def ozon_is_out_of_stock(widget_states: Dict, price_widget: Optional[Dict]) -> bool:
    """
    True, если Ozon показывает, что товар закончился. Цена при этом может
    оставаться в виджете webPrice (тогда у него isAvailable = false).
    """
    if price_widget is not None and price_widget.get("isAvailable") is False:
        return True
    for key, value in widget_states.items():
        if "outofstock" in key.lower():
            return True
        if price_widget is None and isinstance(value, str):
            low = value.lower()
            if any(marker in low for marker in OZON_OUT_OF_STOCK_MARKERS):
                return True
    return False


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
            out_of_stock = ozon_is_out_of_stock(widget_states, price_widget)
            if not price_widget:
                if out_of_stock:
                    last_error = "Товар закончился (цены в ответе нет — покажем последнюю известную)"
                else:
                    last_error = "В ответе не найден виджет с ценой (возможно, товар недоступен или снят с продажи)"
                logger.warning(last_error)
                return {
                    "article": article,
                    "name": find_product_name(widget_states),
                    "price": 0,
                    "card_price": 0,
                    "original_price": 0,
                    "success": False,
                    "out_of_stock": out_of_stock,
                    "error": last_error,
                }

            result = {
                "article": article,
                "name": find_product_name(widget_states),
                "price": extract_price_number(price_widget.get("price", "")),
                "card_price": extract_price_number(price_widget.get("cardPrice", "")),
                "original_price": extract_price_number(price_widget.get("originalPrice", "")),
                "in_stock": not out_of_stock,
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


# ---------------------------------------------------------------------------
# Wildberries: прямой запрос к внутреннему API сайта (без браузера)
# ---------------------------------------------------------------------------
# Это тот же запрос, который делает сам сайт при открытии карточки товара:
#     https://www.wildberries.ru/__internal/u-card/cards/v4/detail?...&nm=<артикул>
# Старый card.wb.ru закрыт (403). Новый эндпоинт пускает запрос, только если
# есть cookie x_wbaas_token И заголовок `deviceid` (проверено: без deviceid —
# 403, с ним — 200). Несколько артикулов можно запросить одним вызовом,
# перечислив их в nm через ';'.
#
# Токен x_wbaas_token получаем так же, как в проекте wb_parse_search_phrase
# (github.com/Duff89/wb_parse_search_phrase, get_token.py):
#   1. открываем https://www.wildberries.ru/ через seleniumbase в режиме
#      uc=True (undetected Chrome, без окна) — этот режим проходит антибот WB;
#   2. забираем cookie x_wbaas_token через CDP (Network.getAllCookies);
#   3. браузер закрываем, дальше работаем обычными HTTP-запросами.
# deviceid — случайный идентификатор вида site_<32 hex>, генерируется сам.
#
# Свежий токен берётся при каждом запуске программы, а также автоматически,
# если WB ответил 498/403 (токен протух или сменился IP — токен к нему привязан).
#
# Нужен пакет:  pip install seleniumbase

# Запасной токен — используется, только если браузер не смог получить новый.
WB_X_WBAAS_TOKEN = ''
WB_DEST = '-8234381'   # регион доставки — от него зависит цена
WB_TOKEN_FILE = ".wbaas_token"
WB_API_URL = "https://www.wildberries.ru/__internal/u-card/cards/v4/detail"
WB_HOME_URL = "https://www.wildberries.ru/"
WB_PRICE_SOURCE = "api"  # метка в price_state.json: цена получена через API
WB_BATCH_SIZE = 50     # сколько артикулов отправлять в одном запросе
WB_TOKEN_ATTEMPTS = 6               # попыток найти cookie в браузере...
WB_TOKEN_ATTEMPT_PAUSE = 5          # ...с паузой между ними (сек)
WB_REFRESH_COOLDOWN_SECONDS = 300   # не обновлять токен чаще, чем раз в 5 минут
WB_AUTH_FAIL_CODES = (401, 403, 498)
# 4. Не бежать за новым токеном при первом же отказе: сначала пауза и повтор
#    со старым токеном (отказ бывает разовым), и только потом — браузер.
WB_AUTH_RETRY_DELAY_MIN = 30
WB_AUTH_RETRY_DELAY_MAX = 60
# Один и тот же User-Agent и для браузера, и для запросов — токен выдаётся под него.
WB_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36'
)

try:
    from seleniumbase import Driver as SBDriver
    SELENIUMBASE_AVAILABLE = True
except ImportError:
    SBDriver = None
    SELENIUMBASE_AVAILABLE = False


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


def _decode_wbaas_token(token: str) -> dict:
    """
    Достаёт из токена IP, User-Agent и срок действия. Формат:
    1.1000.<id>.<base64: ?|IP|UA|expires|...>.<подпись>
    """
    try:
        payload = token.split(".")[3]
        payload += "=" * (-len(payload) % 4)
        parts = base64.b64decode(payload).decode("utf-8", "replace").split("|")
        return {
            "ip": parts[1] if len(parts) > 1 else "",
            "user_agent": parts[2] if len(parts) > 2 else "",
            "expires": int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0,
        }
    except Exception:
        return {"ip": "", "user_agent": "", "expires": 0}


def _generate_wb_device_id() -> str:
    """Как в common_data.py референсного проекта: site_ + 32 hex-символа."""
    return f"site_{uuid.uuid4().hex}"


def _load_wb_auth() -> dict:
    """Читает сохранённые токен и deviceid из .wbaas_token."""
    data = {}
    path = Path(WB_TOKEN_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"WB: не удалось прочитать {WB_TOKEN_FILE}: {e}")
    return {
        "token": data.get("token") or WB_X_WBAAS_TOKEN,
        "device_id": data.get("device_id") or _generate_wb_device_id(),
    }


def _save_wb_auth(token: str, device_id: str) -> None:
    info = _decode_wbaas_token(token)
    expires_at = info["expires"] * 1000 if info["expires"] else int((time.time() + 3 * 86400) * 1000)
    data = {"token": token, "expires_at": expires_at, "device_id": device_id}
    try:
        Path(WB_TOKEN_FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"WB: не удалось сохранить {WB_TOKEN_FILE}: {e}")


_wb_session: Optional[requests.Session] = None
_wb_last_refresh_at = 0.0


def _build_wb_session(token: str, device_id: str) -> requests.Session:
    # User-Agent должен совпадать с тем, под который выдан токен.
    user_agent = _decode_wbaas_token(token)["user_agent"] or WB_USER_AGENT
    s = requests.Session()
    s.headers.update({
        'accept': '*/*',
        'accept-language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'deviceid': device_id,
        'priority': 'u=1, i',
        'sec-ch-ua': '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': user_agent,
        'x-requested-with': 'XMLHttpRequest',
        'x-spa-version': '14.26.3',
        'x-userid': '0',
    })
    if token:
        s.cookies.set('x_wbaas_token', token, domain='.wildberries.ru')
    return s


def _get_wb_session() -> requests.Session:
    """
    Одна HTTP-сессия на весь прогон программы. При первом обращении — как в
    референсном проекте — сразу берём свежий токен через браузер; если не
    вышло, пробуем сохранённый в .wbaas_token.
    """
    global _wb_session
    if _wb_session is None:
        if not _refresh_wb_token_via_browser():
            auth = _load_wb_auth()
            logger.warning("WB: использую сохранённый токен из .wbaas_token (может быть устаревшим).")
            _wb_session = _build_wb_session(auth["token"], auth["device_id"])
    return _wb_session


def _get_token_seleniumbase(headless: bool) -> Optional[str]:
    """Точная копия подхода get_token.py: uc-режим + cookie через CDP."""
    driver = SBDriver(uc=True, headed=not headless, headless=headless, agent=WB_USER_AGENT)
    try:
        driver.open(WB_HOME_URL)
        for _ in range(WB_TOKEN_ATTEMPTS):
            cookies = driver.execute_cdp_cmd("Network.getAllCookies", {})
            for cookie in cookies.get("cookies", []):
                if cookie.get("name") == "x_wbaas_token" and cookie.get("value"):
                    return cookie["value"]
            time.sleep(WB_TOKEN_ATTEMPT_PAUSE)
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _get_token_plain_selenium() -> Optional[str]:
    """Запасной вариант, если seleniumbase не установлен: обычный Chrome из create_driver."""
    driver = create_driver(headless=True)
    try:
        driver.get(WB_HOME_URL)
        for _ in range(WB_TOKEN_ATTEMPTS):
            cookie = driver.get_cookie("x_wbaas_token")
            if cookie and cookie.get("value"):
                return cookie["value"]
            time.sleep(WB_TOKEN_ATTEMPT_PAUSE)
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _refresh_wb_token_via_browser() -> bool:
    """
    Получает свежий x_wbaas_token через браузер и пересоздаёт HTTP-сессию.
    Возвращает True, если токен обновлён.
    """
    global _wb_session, _wb_last_refresh_at
    if time.time() - _wb_last_refresh_at < WB_REFRESH_COOLDOWN_SECONDS:
        return False
    _wb_last_refresh_at = time.time()

    logger.info("WB: получаю свежий токен через браузер...")
    token = None
    attempts = ([("seleniumbase uc без окна", lambda: _get_token_seleniumbase(headless=True)),
                 ("seleniumbase uc с окном", lambda: _get_token_seleniumbase(headless=False))]
                if SELENIUMBASE_AVAILABLE else
                [("обычный selenium", _get_token_plain_selenium)])
    if not SELENIUMBASE_AVAILABLE:
        logger.warning("WB: seleniumbase не установлен (pip install seleniumbase) — "
                       "пробую обычный selenium, он проходит антибот WB хуже.")

    for label, fn in attempts:
        try:
            token = fn()
        except Exception as e:
            logger.warning(f"WB: ошибка браузера ({label}): {e}")
            token = None
        if token:
            break
        logger.warning(f"WB: токен не получен ({label}).")

    if not token:
        logger.warning("WB: не удалось получить токен через браузер.")
        return False

    device_id = _load_wb_auth()["device_id"]
    _save_wb_auth(token, device_id)
    _wb_session = _build_wb_session(token, device_id)
    info = _decode_wbaas_token(token)
    logger.info(f"WB: токен получен (IP {info['ip']}), сохранён в {WB_TOKEN_FILE}.")
    return True


def _parse_wb_product(p: dict) -> dict:
    """
    Цены в ответе — в копейках: price.product — цена со скидкой (то, что
    видит покупатель), price.basic — зачёркнутая цена. У разных размеров цена
    может отличаться, берём минимальную среди размеров в наличии.
    """
    article = str(p.get("id", ""))
    brand = p.get("brand", "")
    name = p.get("name", "")
    full_name = f"{brand} / {name}" if brand else name

    sizes_with_price = [s for s in p.get("sizes", []) if s.get("price")]
    stock_qty = sum(st.get("qty", 0) for s in p.get("sizes", []) for st in s.get("stocks", []) or [])
    if not sizes_with_price:
        # Распроданный товар WB отдаёт без цены — покажем последнюю известную.
        result = _wb_error(article, "Товар закончился (цены в ответе нет — покажем последнюю известную)")
        result["name"] = full_name
        result["out_of_stock"] = True
        return result

    price = min(s["price"].get("product", 0) for s in sizes_with_price) // 100
    original_price = max(s["price"].get("basic", 0) for s in sizes_with_price) // 100

    return {
        "article": article,
        "name": full_name,
        "price": price,
        "card_price": 0,
        "original_price": original_price if original_price != price else 0,
        "in_stock": stock_qty > 0,
        "success": price > 0,
        "error": "" if price > 0 else "WB вернул нулевую цену",
    }


def get_prices_batch_wb(articles: list[str]) -> dict[str, dict]:
    """
    Получает цены WB для списка артикулов прямыми HTTP-запросами
    (по WB_BATCH_SIZE артикулов за запрос). Возвращает {артикул: результат}.
    """
    clean_articles = list(dict.fromkeys(str(a).strip() for a in articles if str(a).strip()))
    results: Dict[str, dict] = {}
    if not clean_articles:
        return results

    session = _get_wb_session()

    for start in range(0, len(clean_articles), WB_BATCH_SIZE):
        chunk = clean_articles[start:start + WB_BATCH_SIZE]
        params = {
            'appType': '1',
            'curr': 'rub',
            'dest': WB_DEST,
            'spp': '30',
            'hide_vflags': '4294967296',
            'hide_dflags': '1048576',
            'mtype': '257',
            'lang': 'ru',
            'ab_testing': 'false',
            'nm': ';'.join(chunk),
        }
        headers = {'referer': WB_PRODUCT_URL_TEMPLATE.format(article=chunk[0])}

        try:
            response = session.get(WB_API_URL, params=params, headers=headers, timeout=20)
            # 498/403 — токен протух или сменился IP. Сначала ждём и пробуем ещё раз
            # со старым токеном; если снова отказ — обновляем токен через браузер.
            if response.status_code in WB_AUTH_FAIL_CODES:
                delay = random.randint(WB_AUTH_RETRY_DELAY_MIN, WB_AUTH_RETRY_DELAY_MAX)
                logger.warning(f"WB: HTTP {response.status_code} — жду {delay} с и пробую ещё раз с тем же токеном.")
                time.sleep(delay)
                response = session.get(WB_API_URL, params=params, headers=headers, timeout=20)
            if response.status_code in WB_AUTH_FAIL_CODES:
                logger.warning(f"WB: снова HTTP {response.status_code} — токен недействителен, обновляю.")
                if _refresh_wb_token_via_browser():
                    session = _get_wb_session()
                    response = session.get(WB_API_URL, params=params, headers=headers, timeout=20)
        except requests.RequestException as e:
            for a in chunk:
                results[a] = _wb_error(a, f"Ошибка сети при запросе к WB: {e}")
            continue

        if response.status_code != 200:
            if response.status_code in WB_AUTH_FAIL_CODES:
                error = (f"WB ответил HTTP {response.status_code} — токен недействителен, "
                         f"обновить его не удалось (или уже пробовали <5 мин назад) — см. лог выше")
            elif response.status_code == 429:
                error = "WB ответил HTTP 429 — слишком частые запросы, попробуем в следующем цикле"
            else:
                error = f"WB ответил HTTP {response.status_code}"
            logger.warning(f"WB: {error}")
            for a in chunk:
                results[a] = _wb_error(a, error)
            continue

        try:
            products = response.json().get("products", [])
        except ValueError as e:
            for a in chunk:
                results[a] = _wb_error(a, f"Не удалось разобрать ответ WB: {e}")
            continue

        for p in products:
            parsed = _parse_wb_product(p)
            if parsed["article"] in chunk:
                results[parsed["article"]] = parsed

        for a in chunk:
            if a not in results:
                results[a] = _wb_error(a, "Товар не найден или снят с продажи")

        if start + WB_BATCH_SIZE < len(clean_articles):
            time.sleep(random.uniform(1.5, 3.0))

    return results


def get_price_by_article_wb(article: str) -> dict:
    """Проверка одного артикула WB вне общего цикла мониторинга."""
    article = str(article).strip()
    return get_prices_batch_wb([article]).get(
        article, _wb_error(article, "Ошибка получения данных")
    )


CHECK_INTERVAL_SECONDS = 120  # базовый интервал (используется, если RANDOMIZE_INTERVAL = False)

# --- «Человеческий» ритм проверок, чтобы не выглядеть для антибота как робот ---
# 1. Случайный интервал между кругами вместо ровных 2 минут.
RANDOMIZE_INTERVAL = True
INTERVAL_MIN_SECONDS = 90
INTERVAL_MAX_SECONDS = 180
# 2. Ночью проверяем реже (часы по времени компьютера; начало включительно, конец — нет).
NIGHT_START_HOUR = 1
NIGHT_END_HOUR = 8
NIGHT_INTERVAL_MIN_SECONDS = 600    # 10 минут
NIGHT_INTERVAL_MAX_SECONDS = 900    # 15 минут
# 3. Иногда длинная пауза, «человек отошёл». Шанс на каждом дневном круге.
LONG_PAUSE_CHANCE = 0.15            # ~ раз в 6-7 кругов
LONG_PAUSE_MIN_SECONDS = 300        # 5 минут
LONG_PAUSE_MAX_SECONDS = 600        # 10 минут

# Временные переключатели площадок. False — площадка пропускается в каждом круге
# (артикулы в файле остаются, бот ими управлять может, просто проверка не идёт).
OZON_ENABLED = True
WB_ENABLED = True
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
    # Это зачёркнутая цена на сайте, а не цена предыдущей итерации.
    ("original_price", "Цена до скидки"),
]

# Для WB уведомляем только об изменении фактической цены продажи. Поле
# original_price остаётся справочным и не участвует в сравнении итераций.
WB_TRACKED_PRICE_FIELDS = {"price"}

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


def choose_next_interval() -> tuple:
    """Возвращает (секунды до следующего круга, пояснение для консоли)."""
    if not RANDOMIZE_INTERVAL:
        return CHECK_INTERVAL_SECONDS, "фиксированный интервал"

    hour = time.localtime().tm_hour
    if NIGHT_START_HOUR <= hour < NIGHT_END_HOUR:
        return random.randint(NIGHT_INTERVAL_MIN_SECONDS, NIGHT_INTERVAL_MAX_SECONDS), "ночной режим"

    if random.random() < LONG_PAUSE_CHANCE:
        return random.randint(LONG_PAUSE_MIN_SECONDS, LONG_PAUSE_MAX_SECONDS), "длинная пауза"

    return random.randint(INTERVAL_MIN_SECONDS, INTERVAL_MAX_SECONDS), "обычный интервал"


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


PRICES_BUTTON = "💰 Цены"
TG_MESSAGE_LIMIT = 4000      # у Telegram лимит 4096 символов на сообщение
PRICE_LIST_NAME_MAX = 60     # длинные названия обрезаем, чтобы список читался


def build_main_menu_keyboard() -> ReplyKeyboardMarkup:
    marketplace_row = [KeyboardButton(text=mp["menu_button"]) for mp in MARKETPLACES.values()]
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Запустить"), KeyboardButton(text="⏸ Остановить")],
            marketplace_row,
            [KeyboardButton(text=PRICES_BUTTON)],
        ],
        resize_keyboard=True,
    )


def format_rub(value) -> str:
    """93495 -> '93 495 ₽'."""
    return f"{int(value):,}".replace(",", " ") + " ₽"


def build_prices_messages(price_state: Dict[str, Dict], articles_by_mp: Dict[str, List[str]]) -> List[str]:
    """
    Список всех отслеживаемых артикулов Ozon и WB с последней ценой из
    price_state.json. Если товар закончился — цена (последняя известная)
    и пометка. Возвращает список сообщений (длинный список режется на части).
    """
    enabled = {"ozon": OZON_ENABLED, "wb": WB_ENABLED}
    blocks: List[str] = ["💰 Актуальные цены"]

    for mp_key, mp in MARKETPLACES.items():
        articles = articles_by_mp.get(mp_key, [])
        header = f"\n📦 {mp['label']} ({len(articles)})"
        if not enabled.get(mp_key, True):
            header += " — проверка сейчас выключена, цены могут быть старыми"
        blocks.append(header)

        if not articles:
            blocks.append("   список пуст")
            continue

        for i, article in enumerate(articles, start=1):
            data = price_state.get(f"{mp_key}:{article}") or {}
            name = data.get("name") or "название появится после первой проверки"
            if len(name) > PRICE_LIST_NAME_MAX:
                name = name[:PRICE_LIST_NAME_MAX - 1].rstrip() + "…"

            price = data.get("price")
            in_stock = data.get("in_stock", True)

            if price:
                price_line = format_rub(price)
                if data.get("card_price"):
                    price_line += f" (по карте {format_rub(data['card_price'])})"
            elif data:
                price_line = "цена неизвестна"
            else:
                price_line = "ещё не проверялся"

            if not in_stock:
                price_line += " — ❌ товар закончился"
                if price and data.get("price_stale"):
                    price_line += " (последняя известная цена)"

            checked = data.get("last_checked", "")
            meta = f"арт. {article}" + (f" · проверено {checked[:16]}" if checked else "")
            blocks.append(f"{i}. {name}\n   {price_line}\n   {meta}")

    # Режем на сообщения по лимиту Telegram, не разрывая карточку товара.
    messages: List[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n{block}" if current else block
        if len(candidate) > TG_MESSAGE_LIMIT and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


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
        "in_stock": result.get("in_stock", True),
        "last_checked": timestamp,
    }
    in_stock = current_data["in_stock"]
    if not in_stock:
        print("   ⚠️ Товар закончился (цена ниже — та, что показывает площадка)")

    for field_key, field_label in PRICE_FIELDS:
        current_value = result[field_key]
        if not current_value:
            continue

        previous_value = previous_data.get(field_key) if previous_data else None
        line = f"   {field_label}: {current_value} ₽"

        # На Wildberries «Цена до скидки» — маркетинговая зачёркнутая цена.
        # С предыдущей итерацией сравниваем только реальную текущую цену.
        track_change = mp_key != "wb" or field_key in WB_TRACKED_PRICE_FIELDS

        if track_change and previous_value and current_value < previous_value:
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

            # Если товара нет в наличии, «снижение» купить всё равно нельзя — не шлём.
            if in_stock and telegram_config and telegram_config.get("chat_ids"):
                chat_ids = telegram_config["chat_ids"]
                sum(
                    1 for chat_id in chat_ids
                    if send_telegram_message(telegram_config["bot_token"], chat_id, message)
                )

        elif track_change and previous_value and current_value > previous_value:
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

    try:
        while True:
            monitoring_enabled.wait()

            # Последовательность намеренно фиксирована: сначала весь Ozon,
            # затем весь Wildberries.
            with articles_lock:
                ozon_articles = load_articles(MARKETPLACES["ozon"]["articles_file"]) if OZON_ENABLED else []
                wb_articles = load_articles(MARKETPLACES["wb"]["articles_file"]) if WB_ENABLED else []

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
                        # Старые цены WB снимались со страницы в браузере (другой
                        # регион -> другая цена). Не сравниваем с ними, иначе на
                        # первом круге будут ложные «цена снизилась».
                        if mp_key == "wb" and previous_data and previous_data.get("source") != WB_PRICE_SOURCE:
                            previous_data = None

                        # Товар закончился и площадка не отдала цену: сохраняем
                        # последнюю известную цену и помечаем «нет в наличии».
                        if not result.get("success") and result.get("out_of_stock"):
                            saved = dict(price_state.get(state_key) or {})
                            saved["name"] = result.get("name") or saved.get("name", "")
                            saved["in_stock"] = False
                            saved["price_stale"] = True   # цена — с последней проверки, когда товар был
                            saved["last_checked"] = time.strftime("%d.%m.%Y %H:%M:%S")
                            if mp_key == "wb":
                                saved["source"] = WB_PRICE_SOURCE
                            price_state[state_key] = saved
                            save_price_state(state_filepath, price_state)
                            last_price = saved.get("price")
                            print(f"\n[{saved['last_checked']}] [{mp['label']}]\n"
                                  f"⚠️ {saved['name'] or article} ({article}): товар закончился, "
                                  f"последняя известная цена: {f'{last_price} ₽' if last_price else 'неизвестна'}")
                            return

                        current_data = process_and_print(
                            mp_key, mp["label"], mp["product_url_template"],
                            result, previous_data, telegram_config,
                        )

                        if current_data is not None:
                            if mp_key == "wb":
                                current_data["source"] = WB_PRICE_SOURCE
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

                # WB: весь список одним-двумя прямыми HTTP-запросами (без браузера),
                # дальше каждый товар обрабатывается так же, как Ozon:
                # снижение цены -> зелёная строка в консоли + сообщение в Telegram.
                if monitoring_enabled.is_set() and wb_articles:
                    try:
                        wb_results = get_prices_batch_wb(wb_articles)
                    except Exception as e:
                        logger.warning(f"WB: ошибка при получении цен: {e}")
                        wb_results = {a: _wb_error(a, f"Ошибка при запросе к WB: {e}") for a in wb_articles}

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

            pause_seconds, pause_reason = choose_next_interval()
            with monitor_status_lock:
                monitor_status["next_check_at"] = time.time() + pause_seconds
            next_at = time.strftime("%H:%M:%S", time.localtime(time.time() + pause_seconds))
            print(f"\n⏳ Следующая проверка в {next_at} (через {pause_seconds // 60} мин "
                  f"{pause_seconds % 60} с, {pause_reason})")

            sleep_remaining = pause_seconds
            while sleep_remaining > 0:
                if not monitoring_enabled.is_set():
                    break
                chunk = min(1, sleep_remaining)
                time.sleep(chunk)
                sleep_remaining -= chunk

    except KeyboardInterrupt:
        pass


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
        if message.text in menu_buttons | {"▶️ Запустить", "⏸ Остановить", PRICES_BUTTON}:
            await state.clear()
            if message.text == PRICES_BUTTON:
                await send_prices(message.chat.id)
            elif message.text in menu_buttons:
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

    async def send_prices(chat_id: int) -> None:
        with articles_lock:
            articles_by_mp = {k: load_articles(mp["articles_file"]) for k, mp in MARKETPLACES.items()}
        price_state = load_price_state(PRICE_STATE_FILE_DEFAULT)
        for text in build_prices_messages(price_state, articles_by_mp):
            await bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True)

    @dp.message(F.text == PRICES_BUTTON)
    async def btn_prices(message: Message):
        if not is_authorized(message):
            return
        await send_prices(message.chat.id)

    @dp.message(Command("prices"))
    async def cmd_prices(message: Message):
        if not is_authorized(message):
            return
        await send_prices(message.chat.id)

    @dp.message(Command("status"))
    async def cmd_status(message: Message):
        if not is_authorized(message):
            return
        await message.answer(get_status_text())

    await dp.start_polling(bot)


SCRIPT_VERSION = "WB-api-v11 (кнопка «Цены» + пометка «товар закончился»)"


def main():
    print(f"🔖 Запущена версия скрипта: {SCRIPT_VERSION}", flush=True)
    print(f"🔖 Файл: {os.path.abspath(__file__)}", flush=True)
    print(
        f"🔖 seleniumbase (токен WB): {'установлен' if SELENIUMBASE_AVAILABLE else 'НЕ установлен — pip install seleniumbase'}",
        flush=True,
    )
    print(
        f"🔖 Площадки: Ozon {'ВКЛ' if OZON_ENABLED else 'выкл'}, WB {'ВКЛ' if WB_ENABLED else 'выкл'} "
        f"(переключатели OZON_ENABLED / WB_ENABLED рядом с CHECK_INTERVAL_SECONDS)",
        flush=True,
    )
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
