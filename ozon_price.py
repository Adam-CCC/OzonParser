#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ozon Price Checker
------------------
Простая утилита: получает цену товара Ozon по его артикулу.

Использование:
    python ozon_price.py 123456789
    python ozon_price.py            # спросит артикул интерактивно

Требования:
    pip install selenium selenium-stealth
    Установленный Google Chrome (версия должна совпадать с chromedriver,
    Selenium 4.15+ обычно подтягивает драйвер автоматически).
"""

import sys
import json
import re
import time
import logging
from typing import Optional, Dict

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import WebDriverException, TimeoutException
from selenium_stealth import stealth

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


def print_result(result: Dict) -> None:
    """Печатает результат одной проверки с меткой времени."""
    timestamp = time.strftime("%d.%m.%Y %H:%M:%S")
    print(f"\n[{timestamp}]")

    if result["success"]:
        print(f"✅ Товар: {result['name']}")
        print(f"   Артикул: {result['article']}")
        print(f"   Цена: {result['price']} ₽")
        if result["card_price"]:
            print(f"   Цена по карте Ozon: {result['card_price']} ₽")
        if result["original_price"] and result["original_price"] != result["price"]:
            print(f"   Старая цена: {result['original_price']} ₽")
    else:
        print(f"❌ Не удалось получить цену: {result['error']}")


def monitor_price(article: str, interval_seconds: int = CHECK_INTERVAL_SECONDS) -> None:
    """Бесконечно проверяет цену товара с заданным интервалом, пока не остановят (Ctrl+C)."""
    print(f"🔁 Запущен мониторинг артикула {article}. Проверка каждые {interval_seconds // 60} мин.")
    print("   Останови программу сочетанием Ctrl+C, когда будет нужно.\n")

    last_price: Optional[int] = None

    while True:
        try:
            result = get_price_by_article(article)
            print_result(result)

            if result["success"]:
                if last_price is not None and result["price"] != last_price:
                    diff = result["price"] - last_price
                    arrow = "📈" if diff > 0 else "📉"
                    print(f"   {arrow} Цена изменилась: {last_price} ₽ → {result['price']} ₽ ({diff:+} ₽)")
                last_price = result["price"]

        except Exception as e:
            logger.error(f"Неожиданная ошибка при проверке цены: {e}")

        try:
            print(f"⏳ Следующая проверка через {interval_seconds // 60} мин...")
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("\n🛑 Мониторинг остановлен пользователем.")
            break


def main():
    if len(sys.argv) > 1:
        raw_input_value = sys.argv[1]
    else:
        raw_input_value = input("Введите артикул товара Ozon (или ссылку на товар): ").strip()

    article = extract_article_from_input(raw_input_value)

    if not article.isdigit():
        print(f"❌ Некорректный артикул: {raw_input_value}")
        sys.exit(1)

    try:
        monitor_price(article)
    except KeyboardInterrupt:
        print("\n🛑 Мониторинг остановлен пользователем.")


if __name__ == "__main__":
    main()