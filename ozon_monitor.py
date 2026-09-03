#!/usr/bin/env python3
"""
Ozon Promo Monitor
==================

Следит за одним или несколькими товарами на Ozon (по артикулу или прямой ссылке)
и присылает уведомление в Telegram, когда на товаре появляется акция/скидка,
удовлетворяющая заданному порогу.

Как это работает:
  1. Раз в N минут скрипт открывает страницу товара headless-браузером (Playwright)
     и достаёт текущую и "старую" (зачёркнутую) цену.
  2. Считает процент скидки и сравнивает с порогом из конфига.
  3. Если скидка подходит и уведомление по ней ещё не отправлялось — шлёт сообщение
     в Telegram и запоминает, что уже уведомил (чтобы не спамить на каждой проверке).
  4. Когда акция заканчивается (скидка снова ниже порога) — сбрасывает отметку,
     чтобы следующая акция на этом же товаре снова вызвала уведомление.

ВАЖНО про хрупкость парсинга: у Ozon нет официального публичного API для получения
цены по произвольному артикулу, а вёрстка страницы товара может меняться. Скрипт
ищет блок с ценой по атрибуту data-widget="webPrice" и достаёт из него все суммы
в рублях. Если Ozon изменит вёрстку и это перестанет работать — запусти скрипт
с флагом --debug и посмотри сохранённые HTML/скриншот в папке debug/, чтобы
поправить извлечение под актуальную страницу (см. функцию fetch_price_block_text).

Установка:
    pip install -r requirements.txt
    playwright install chromium

Настройка:
    1. Скопируй config.example.json в config.json и заполни:
       - telegram.bot_token и telegram.chat_id (см. README.md, как их получить)
       - items: список товаров (артикул или прямая ссылка на товар)
    2. Проверка логики без Telegram (токен бота для этого не нужен):
       python ozon_monitor.py --config config.json --once --debug --no-notify
    3. Постоянный мониторинг:
       python ozon_monitor.py --config config.json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PRICE_RE = re.compile(r"(\d[\d\s ]{1,9})\s?₽")  # число + пробелы/nbsp + значок ₽

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ozon_monitor")


# ---------------------------------------------------------------------------
# Модель товара
# ---------------------------------------------------------------------------

@dataclass
class Item:
    name: str
    article: Optional[str]
    url: Optional[str]
    threshold_percent: float
    target_price: Optional[float]

    def resolved_url(self) -> str:
        if self.url:
            return self.url
        return f"https://www.ozon.ru/product/{self.article}/"

    def key(self) -> str:
        return self.article or self.url


def parse_items(config: dict) -> list[Item]:
    default_threshold = config.get("default_threshold_percent", 20)
    items: list[Item] = []
    for raw in config.get("items", []):
        article = raw.get("article")
        url = raw.get("url")
        if not article and not url:
            logger.warning("Пропускаю запись без article и url: %s", raw)
            continue
        items.append(
            Item(
                name=raw.get("name") or article or url,
                article=article,
                url=url,
                threshold_percent=raw.get("threshold_percent") or default_threshold,
                target_price=raw.get("target_price"),
            )
        )
    return items


# ---------------------------------------------------------------------------
# Конфиг и состояние
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        logger.error(
            "Не найден файл конфига %s. Скопируй config.example.json в config.json и заполни его.",
            path,
        )
        sys.exit(1)
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.warning("Файл состояния %s повреждён, начинаю с чистого листа", path)
    return {}


def save_state(path: Path, state: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.error("Telegram вернул ошибку %s: %s", resp.status_code, resp.text)
    except requests.RequestException:
        logger.exception("Не удалось отправить сообщение в Telegram")


def build_notification_text(item: Item, current_price: int, original_price: int, discount_percent: float) -> str:
    lines = [f"\U0001F525 Акция: {item.name}", f"Текущая цена: {current_price} ₽"]
    if original_price != current_price:
        lines.append(f"Была: {original_price} ₽ (скидка {discount_percent:.0f}%)")
    lines.append(item.resolved_url())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Получение цены со страницы товара
# ---------------------------------------------------------------------------

def extract_prices_from_text(text: str) -> list[int]:
    """Достаёт все встречающиеся суммы в рублях из текста."""
    raw = PRICE_RE.findall(text)
    numbers = []
    for r in raw:
        cleaned = r.replace(" ", "").replace(" ", "")
        if cleaned.isdigit():
            numbers.append(int(cleaned))
    return numbers


def is_challenge_page(page: Page) -> bool:
    """Проверяет, показал ли Ozon вместо товара свою антибот-проверку
    (страница на домене abt-challenge, замаскированная под "нет соединения")."""
    try:
        if "abt-challenge" in page.url:
            return True
        title = (page.title() or "").lower()
        if "нет соединения" in title:
            return True
        if page.locator("#reload-button").count() > 0:
            return True
    except Exception:
        pass
    return False


def fetch_price_block_text(page: Page, url: str, debug_dir: Optional[Path], item_key: str) -> str:
    page.goto(url, timeout=30000, wait_until="domcontentloaded")

    for attempt in range(3):
        if not is_challenge_page(page):
            break
        logger.warning(
            "[%s] Ozon показал антибот-проверку вместо страницы товара (попытка %s/3)",
            item_key,
            attempt + 1,
        )
        try:
            reload_btn = page.locator("#reload-button")
            if reload_btn.count() > 0:
                reload_btn.first.click()
            else:
                page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        except Exception:
            logger.exception("[%s] Не удалось перезагрузить страницу после антибот-проверки", item_key)
            break
    else:
        logger.warning(
            "[%s] Антибот-проверка Ozon не снялась за 3 попытки. Если это повторяется постоянно, "
            "попробуй поставить \"headless\": false в config.json — headless-режим браузера "
            "чаще вызывает подозрение у защиты Ozon, чем обычный видимый браузер.",
            item_key,
        )

    try:
        page.wait_for_selector('[data-widget="webPrice"]', timeout=15000)
    except PlaywrightTimeoutError:
        if not is_challenge_page(page):
            logger.warning(
                "[%s] Не нашёл блок цены по селектору data-widget=webPrice за 15 сек — "
                "беру весь текст страницы (возможно, вёрстка Ozon изменилась)",
                item_key,
            )

    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        safe_key = re.sub(r"[^\w.-]+", "_", item_key)
        (debug_dir / f"{safe_key}.html").write_text(page.content(), encoding="utf-8")
        try:
            page.screenshot(path=str(debug_dir / f"{safe_key}.png"))
        except Exception:
            logger.exception("[%s] Не удалось сохранить скриншот", item_key)

    blocks = page.locator('[data-widget="webPrice"]')
    if blocks.count() > 0:
        return blocks.first.inner_text()
    return page.inner_text("body")


def get_prices(page: Page, item: Item, debug_dir: Optional[Path]) -> Optional[tuple[int, int]]:
    text = fetch_price_block_text(page, item.resolved_url(), debug_dir, item.key())
    numbers = extract_prices_from_text(text)
    if not numbers:
        return None
    numbers_sorted = sorted(set(numbers))
    current_price = numbers_sorted[0]
    original_price = numbers_sorted[-1] if len(numbers_sorted) > 1 else current_price
    return current_price, original_price


def evaluate_item(item: Item, current_price: int, original_price: int) -> tuple[bool, float]:
    """Возвращает (подходит_ли_под_акцию, процент_скидки)."""
    discount_percent = 0.0
    if original_price > 0 and original_price != current_price:
        discount_percent = (original_price - current_price) / original_price * 100

    threshold_ok = discount_percent >= item.threshold_percent
    price_ok = item.target_price is not None and current_price <= item.target_price
    return (threshold_ok or price_ok), discount_percent


# ---------------------------------------------------------------------------
# Основной цикл проверки
# ---------------------------------------------------------------------------

def check_all_items(
    page: Page,
    items: list[Item],
    state: dict,
    config: dict,
    debug_dir: Optional[Path],
    notify: bool = True,
) -> bool:
    changed = False
    for item in items:
        key = item.key()
        prev = state.get(key, {"notified": False})
        try:
            result = get_prices(page, item, debug_dir)
        except Exception:
            logger.exception("[%s] Ошибка при получении данных со страницы", key)
            continue

        if result is None:
            logger.warning(
                "[%s] Не удалось найти цену на странице — возможно, изменилась вёрстка "
                "или сработала защита от ботов. Запусти с --debug и посмотри debug/%s.html",
                key,
                key,
            )
            continue

        current_price, original_price = result
        is_good_deal, discount_percent = evaluate_item(item, current_price, original_price)

        logger.info(
            "[%s] цена=%s, было=%s, скидка=%.0f%%, акция_подходит=%s",
            key,
            current_price,
            original_price,
            discount_percent,
            is_good_deal,
        )

        if is_good_deal and not prev.get("notified"):
            text = build_notification_text(item, current_price, original_price, discount_percent)
            if notify:
                telegram_cfg = config.get("telegram", {})
                send_telegram(telegram_cfg.get("bot_token", ""), telegram_cfg.get("chat_id", ""), text)
            else:
                logger.info(
                    "[%s] АКЦИЯ ПОДХОДИТ (уведомление не отправлено — режим проверки без Telegram):\n%s",
                    key,
                    text,
                )
            prev["notified"] = True
            changed = True
        elif not is_good_deal and prev.get("notified"):
            prev["notified"] = False
            changed = True

        prev["last_price"] = current_price
        prev["last_original_price"] = original_price
        prev["last_checked"] = datetime.now().isoformat(timespec="seconds")
        state[key] = prev
        changed = True  # цену/время проверки в любом случае стоит сохранить

    return changed


def run(config_path: str, once: bool, debug: bool, notify: bool = True) -> None:
    config = load_config(config_path)
    items = parse_items(config)
    if not items:
        logger.error("В конфиге нет ни одного корректного товара в items — нечего мониторить")
        sys.exit(1)

    state_path = Path(config.get("state_file", "state.json"))
    state = load_state(state_path)
    debug_dir = Path(config.get("debug_dir", "debug")) if debug else None
    interval_seconds = max(60, int(config.get("poll_interval_minutes", 5) * 60))
    headless = config.get("headless", False)

    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="ru-RU",
                    viewport={"width": 1280, "height": 900},
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
                page = context.new_page()
                try:
                    while True:
                        changed = check_all_items(page, items, state, config, debug_dir, notify=notify)
                        if changed:
                            save_state(state_path, state)
                        if once:
                            return
                        logger.info("Жду %s сек. до следующей проверки...", interval_seconds)
                        time.sleep(interval_seconds)
                finally:
                    browser.close()
        except KeyboardInterrupt:
            logger.info("Остановлено пользователем")
            return
        except Exception:
            logger.exception("Критическая ошибка, перезапускаю браузер через 30 секунд")
            time.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser(description="Мониторинг акций на Ozon по артикулу")
    parser.add_argument("--config", default="config.json", help="Путь до config.json")
    parser.add_argument(
        "--once", action="store_true", help="Сделать один проход и выйти (для проверки настроек)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Сохранять HTML и скриншот страницы товара при каждой проверке в папку debug/",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help=(
            "Не отправлять уведомления в Telegram — только проверить, что программа "
            "правильно находит цену и определяет акцию (для тестирования, токен бота не нужен)"
        ),
    )
    args = parser.parse_args()
    run(args.config, args.once, args.debug, notify=not args.no_notify)


if __name__ == "__main__":
    main()
