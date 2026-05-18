import os
import time
import random

from typing import Type, Optional, Tuple

from database.models import Market
from database.db import DbConnection
from log_api import logger, get_moscow_time
from sqlalchemy.exc import IntegrityError

from playwright.sync_api import (sync_playwright, TimeoutError as PwTimeoutError, Error as PwError,)

TIME_AWAITED = 5
TIME_SLEEP = (10, 15)


def _parse_proxy(proxy: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    В твоём selenium-коде прокси приходил строкой.
    Ожидаемый формат: 'http://login:pass@host:port' (или без логина/пароля).

    Playwright хочет:
      proxy={"server":"http://host:port","username":"login","password":"pass"}
    """
    if not proxy:
        return "", None, None

    if "@" not in proxy:
        # уже может быть 'http://host:port' или 'socks5://host:port'
        return proxy, None, None

    creds, hostport = proxy.split("@", 1)

    scheme = "http"
    rest = creds
    if "://" in creds:
        scheme, rest = creds.split("://", 1)

    if ":" in rest:
        user, pwd = rest.split(":", 1)
    else:
        user, pwd = rest, ""

    server = f"{scheme}://{hostport}"
    return server, user, pwd


class BrowserController:
    """
    Playwright Chromium controller, интерфейс совместим со старым Selenium WebDriver:
      - load_url(url)
      - is_browser_active()
      - quit()
    """

    def __init__(self, market: Market, user: str, db_conn_admin: DbConnection, db_conn_arris: DbConnection):
        # --- Данные/контекст как было ---
        self.user = user
        self.market = market
        self.client_id = market.client_id
        self.db_conn_admin = db_conn_admin
        self.db_conn_arris = db_conn_arris

        self.proxy = market.connect_info.proxy
        self.phone = market.connect_info.phone
        self.browser_id = f"{market.connect_info.phone}_МВидео"
        self.marketplace = market.marketplace_info

        # --- Пути как было ---
        self.profile_path = os.path.join(os.getcwd(), "profile", self.browser_id)

        os.makedirs(self.profile_path, exist_ok=True)

        # --- Playwright start ---
        self._pw = sync_playwright().start()

        # Proxy (нативно)
        server, username, password = _parse_proxy(self.proxy)
        proxy_cfg = None
        if server:
            proxy_cfg = {"server": server}
            if username is not None:
                proxy_cfg["username"] = username
                proxy_cfg["password"] = password or ""

        # Persistent context = профиль на диск (аналог -profile в Firefox)
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile_path,
            headless=True,
            proxy=proxy_cfg,
            locale="ru-RU",
            no_viewport=True,  # ближе к реальному браузеру
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1280,1200",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        # timeouts (НЕ бесконечные, чтобы не зависать навсегда)
        self.context.set_default_timeout(60_000)
        self.context.set_default_navigation_timeout(120_000)

        # page
        pages = self.context.pages
        self.page = pages[0] if pages else self.context.new_page()

    # --- Вспомогательные методы ---
    def _sleep_rand(self):
        time.sleep(random.randint(*TIME_SLEEP))

    # --- Логика авторизации ---
    def check_auth(self) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=TIME_AWAITED * 4 * 1000)
            self.page.wait_for_timeout(3000)

            current_url = self.page.url or ""

            # Если форма ввода телефона видна — нужна авторизация
            login_input = self.page.locator("input[name='phone']")
            try:
                login_input.wait_for(state="visible", timeout=3000)
                login_form_visible = True
            except PwTimeoutError:
                login_form_visible = False

            if login_form_visible:
                logger.info(
                    f"Автоматизация {self.market.name_company} запущена "
                    f"(URL: {current_url})"
                )
                if self.marketplace.marketplace == "МВидео":
                    self.mvideo_auth(self.marketplace)

            elif self.marketplace.domain in current_url:
                logger.info(f"Вход в ЛК {self.market.name_company} выполнен")

            else:
                logger.warning(
                    f"{self.market.name_company}: неизвестная страница: {current_url}"
                )

        except Exception as e:
            logger.error(
                f"Ошибка автоматизации {self.market.name_company}: {e}",
                exc_info=True,
            )
            self.quit()



    # --- API как в main.py ---
    def is_browser_active(self):
        try:
            return (self.page is not None) and (not self.page.is_closed()) and bool(self.page.url)
        except Exception:
            return False

    def load_url(self, url: str):
        if self.client_id is None:
            self.quit(f"{self.market.name_company} {self.market.entrepreneur} не обнаружен в client_id")
            return

        logger.info(f"Авторизация {self.market.name_company}")
        self.page.goto(url, wait_until="load", timeout=TIME_AWAITED * 4 * 1000)
        self.check_auth()

    def quit(self, text: str = None):
        if text:
            logger.error(f"{text}")
        else:
            logger.info(f"Браузер для {self.market.name_company} закрыт")

        try:
            if getattr(self, "context", None):
                self.context.close()
        finally:
            try:
                if getattr(self, "_pw", None):
                    self._pw.stop()
            except Exception:
                pass

    def mvideo_auth(self, marketplace) -> bool | None:
        """Авторизация в МВидео по номеру телефона через Playwright."""

        page = self.page
        db_conn = self.db_conn_admin

        def log_info(text: str) -> None:
            logger.info(f"{self.market.name_company}: {text}")

        def log_warning(text: str) -> None:
            logger.warning(f"{self.market.name_company}: {text}")

        def check_login() -> bool:
            current_url = (page.url or "").rstrip("/")

            if current_url == "https://sellers.mvideo.ru/mpa":
                target_url = f"{self.marketplace.domain.rstrip('/')}/{self.client_id}/marketplace"
                page.goto(
                    target_url,
                    wait_until="load",
                    timeout=TIME_AWAITED * 4 * 1000,
                )
                log_info("Вход в ЛК выполнен")
                return True

            if self.marketplace.domain in current_url:
                log_info("Вход в ЛК выполнен")
                return True

            return False

        def wait_and_enter_code(time_request) -> None:
            log_info(f"Ожидание кода на номер {self.phone}")

            for _ in range(3):
                try:
                    db_conn.add_phone_message(
                        user=self.user,
                        phone=self.phone,
                        marketplace=marketplace.marketplace,
                        time_request=time_request,
                    )
                    break
                except IntegrityError:
                    db_conn.session.rollback()
                    time.sleep(TIME_AWAITED)
            else:
                raise Exception("Ошибка параллельных запросов")

            mes = db_conn.get_phone_message(
                user=self.user,
                phone=self.phone,
                marketplace=marketplace.marketplace,
            )

            code = "".join(ch for ch in mes if ch.isdigit())

            if not code:
                raise Exception("Код подтверждения пустой или не содержит цифр")

            log_info(f"Код на номер {self.phone} получен: {code}")
            log_info(f"Ввод кода {code}")

            input_code = page.locator("mpa-ui-input[formcontrolname='code'] input")
            input_code.wait_for(
                state="visible",
                timeout=TIME_AWAITED * 2 * 1000,
            )
            input_code.fill(code)

            button_confirm = page.get_by_role("button", name="Подтвердить")
            button_confirm.wait_for(
                state="visible",
                timeout=TIME_AWAITED * 2 * 1000,
            )

            log_info("Нажимаем на кнопку Подтвердить")
            button_confirm.click(timeout=TIME_AWAITED * 2 * 1000)

            try:
                page.wait_for_load_state("load", timeout=TIME_AWAITED * 4 * 1000)
            except PwTimeoutError:
                pass

            time.sleep(TIME_AWAITED)

            if not check_login():
                current_url = page.url or ""
                log_warning(f"После ввода кода вход не подтверждён. Текущий URL: {current_url}")

        for attempt in range(3):
            try:
                time.sleep(TIME_AWAITED)

                log_info(f"Ввод номера телефона {self.phone}")

                input_phone = page.locator("input[name='phone']")
                input_phone.wait_for(
                    state="visible",
                    timeout=TIME_AWAITED * 4 * 1000,
                )

                input_phone.fill("")
                input_phone.fill(self.phone)

                log_info("Нажимаем кнопку Войти")

                time_request = get_moscow_time()

                button_login = page.get_by_role("button", name="Войти")
                button_login.wait_for(
                    state="visible",
                    timeout=TIME_AWAITED * 4 * 1000,
                )

                db_conn.check_phone_message(
                    user=self.user,
                    phone=self.phone,
                    time_request=time_request,
                )

                time.sleep(TIME_AWAITED)

                button_login.click(timeout=TIME_AWAITED * 4 * 1000)

                log_info("Номер телефона введён, кнопка Войти нажата")

                wait_and_enter_code(time_request)
                return True

            except PwTimeoutError:
                log_info(
                    f"Не удалось найти поле телефона или кнопку Войти, "
                    f"повторная попытка {attempt + 1}/3"
                )

            except PwError as e:
                log_warning(f"Ошибка Playwright при авторизации: {e}")

            except Exception:
                raise

        raise Exception("Страница авторизации МВидео не получена")
