import os
import re
import json
import time
import requests
import pandas as pd

from datetime import datetime, timedelta, date

from database.data_classes import (
    DataMvideoDistribution,
    DataMvideoAcquiring,
    DataMvideoStock,
)
from log_api.log import logger, get_moscow_time


# (serviceType, taskType, имя файла)
SERVICE_TYPES: list[tuple[str, str, str]] = [
    ("DISTRIBUTION", "BILLING_SUPPLIER_DISTRIBUTION", "distribution.xlsx"),
    ("STORAGE",      "BILLING_SUPPLIER_STORAGE",      "storage.xlsx"),
    ("ACQUIRING",    "BILLING_SUPPLIER_ACQUIRING",    "acquiring.xlsx"),
    ("COMMISSION",   "BILLING_SUPPLIER_COMMISSION",   "commission.xlsx"),
]


class MvideoReports:
    """HTTP-клиент для billing-отчётов МВидео через внутренние API."""

    BASE = "https://sellers.mvideo.ru"

    def __init__(self, driver=None, db_arris=None, market=None) -> None:
        """
        Если передан driver — берём page/context/market из него (онлайн-режим).
        Если driver=None — работаем в оффлайн-режиме (только парсинг локальных файлов),
        в этом случае нужно передать market отдельно.
        """
        self.driver = driver
        if driver is not None:
            self.page = driver.page
            self.context = driver.context
            self.market = driver.market
        else:
            self.page = None
            self.context = None
            self.market = market
        self.db_arris = db_arris
        self._token: str | None = None

    # --- логирование ---
    def _info(self, msg: str) -> None:
        logger.info(f"{self.market.name_company}: {msg}")

    def _error(self, msg: str) -> None:
        logger.error(f"{self.market.name_company}: {msg}")

    # --- токен ---
    def _get_token(self, force: bool = False) -> str | None:
        if self._token and not force:
            return self._token
        try:
            kauth = self.page.evaluate('() => localStorage.getItem("kauth")')
            self._token = json.loads(kauth or "{}").get("accessToken")
            if not self._token:
                self._error("accessToken не найден в localStorage kauth")
            return self._token
        except Exception as e:
            self._error(f"ошибка получения accessToken: {e}")
            return None

    # --- сессия ---
    def _build_session(self, referer: str) -> requests.Session | None:
        token = self._get_token()
        if not token:
            return None
        s = requests.Session()
        for c in self.context.cookies():
            s.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "Origin": self.BASE,
            "Authorization": f"Bearer {token}",
        })
        return s

    # === основная логика ===

    def download_billing_reports_accumulating(
            self,
            download_dir: str = "report",
            poll_interval_s: int = 30,
            max_wait_s: int = 360,
    ) -> list[str]:
        """
        Скачивает billing-отчёты (distribution, storage, acquiring)
        для всех периодов со статусом ACCUMULATING.

        Алгоритм:
        1. GET список периодов /api/v1/billing/distribution/periods/supplier
        2. Фильтруем по status='ACCUMULATING'
        3. Для каждого периода:
           a) POST 4 запроса на формирование (по одному на каждый serviceType)
           b) Поллим /api/report/task/search, пока для всех 4 task'ов
              не появится status='FINISH' (тогда скачиваем) или 'NO_DATA' (пропускаем)
           c) GET /api/report/task/report/{id} — скачиваем готовые xlsx
        Сохраняет файлы в report/<name_company>/<today_msk>/<service>.xlsx
        """
        referer = f"{self.BASE}/mpa/billing/reports/distribution"
        session = self._build_session(referer)
        if session is None:
            return []

        # 1. Список периодов
        periods = self._http_get_json(
            session,
            f"{self.BASE}/api/v1/billing/distribution/periods/supplier",
            params={"page": 0, "size": 50, "sort": ""},
        )
        if not periods:
            self._error("список периодов не получен")
            return []

        accumulating = [
            p for p in periods.get("content", [])
            if p.get("status") == "ACCUMULATING"
        ]
        self._info(f"Найдено периодов ACCUMULATING: {len(accumulating)}")
        if not accumulating:
            return []

        saved: list[str] = []
        for period in accumulating:
            start_date = period["startDate"]

            # 2. Фиксируем московское время до запросов (минус минута для подстраховки)
            request_time = get_moscow_time() - timedelta(minutes=1)
            self._info(
                f"Инициирую формирование отчётов за {start_date} "
                f"(request_time MSK: {request_time:%Y-%m-%d %H:%M:%S})"
            )

            # 3. Запускаем формирование всех 4 типов
            for service_type, _, _ in SERVICE_TYPES:
                ok = self._http_post(
                    session,
                    f"{self.BASE}/api/v1/rd/billing/report/supplier/{self.market.client_id}",
                    params={
                        "serviceType": service_type,
                        "date": start_date,
                        "closed": "false",
                    },
                )
                if ok:
                    self._info(f"  → {service_type}: формирование запущено")
                else:
                    self._error(f"  → {service_type}: ошибка инициации")

            # 4. Поллим все 4 task'а одновременно
            results = self._poll_all_tasks(
                session=session,
                start_date=start_date,
                request_time=request_time,
                poll_interval_s=poll_interval_s,
                max_wait_s=max_wait_s,
            )

            # 5. Скачиваем готовые
            for service_type, _, _ in SERVICE_TYPES:
                task = results.get(service_type)
                if task is None:
                    continue  # NO_DATA, ошибка или таймаут
                path = self._download_task_file(
                    session=session,
                    task=task,
                    download_dir=download_dir,
                )
                if not path:
                    continue
                saved.append(path)

                # DISTRIBUTION — парсим сводную страницу и пишем в БД
                if service_type == "DISTRIBUTION" and self.db_arris is not None:
                    try:
                        rows = self._parse_distribution_xlsx(path)
                        if rows:
                            self.db_arris.add_mvideo_distributions(rows)
                    except Exception as e:
                        self._error(f"ошибка парсинга/записи distribution: {e}")

                # ACQUIRING — парсим детализацию по чекам и пишем в БД
                if service_type == "ACQUIRING" and self.db_arris is not None:
                    try:
                        acquiring_rows = self._parse_acquiring_xlsx(path)
                        if acquiring_rows:
                            self.db_arris.add_mvideo_acquirings(acquiring_rows)
                    except Exception as e:
                        self._error(f"ошибка парсинга/записи acquiring: {e}")

        return saved

    # === локальный парсинг уже скачанных файлов ===

    # Подстроки, по которым ищем xlsx-файлы в локальной папке.
    # Поиск — case-insensitive, по подстроке в имени файла.
    # Также поддерживаются исходные «короткие» имена от пайплайна (distribution/acquiring/storage).
    _LOCAL_REPORT_PATTERNS: dict[str, tuple[str, ...]] = {
        "distribution": (
            "Распределение",
            "дистрибуции",
            "дистрибуция",
            "distribution",
        ),
        "acquiring": (
            "Эквайринг",
            "Проведению расчетов",
            "Проведение расчетов",
            "расчетов",
            "acquiring",
        ),
        "storage": (
            "Услуга хранения",
            "Хранение",
            "storage",
        ),
    }

    def parse_local_directory(
            self,
            directory: str,
            period_date: date,
    ) -> dict[str, int]:
        """
        Парсит уже скачанные xlsx-отчёты из указанной папки и пишет их в БД.

        Ищет файлы по подстроке в имени (case-insensitive):
            - 'Распределение'  или 'distribution' → mv_distribution
            - 'Эквайринг'      или 'acquiring'    → mv_acquiring
            - 'Услуга хранения'/'Хранение' или 'storage' → mv_stocks

        Если по подстроке найдено несколько файлов — берётся первый, остальные
        логируются как предупреждение. Если файлов нет — соответствующий
        тип отчёта пропускается.

        Не требует driver/браузер — работает только с локальными файлами и БД.

        Возвращает dict с количеством записанных строк по каждому типу отчёта.
        """
        if self.market is None:
            self._error_global("parse_local_directory: market не задан")
            return {}

        if self.db_arris is None:
            self._error("parse_local_directory: db_arris не передан, запись в БД невозможна")
            return {}

        if not os.path.isdir(directory):
            self._error(f"parse_local_directory: папка не найдена: {directory}")
            return {}

        self._info(
            f"Локальный парсинг папки: {directory} (period_date={period_date:%Y-%m-%d})"
        )

        # Все xlsx-файлы в папке (один раз)
        all_xlsx = [
            f for f in os.listdir(directory)
            if f.lower().endswith(".xlsx") and os.path.isfile(os.path.join(directory, f))
        ]

        result: dict[str, int] = {
            "distribution": 0,
            "acquiring": 0,
            "storage": 0,
        }

        # DISTRIBUTION
        dist_path = self._find_local_report(directory, all_xlsx, "distribution")
        if dist_path is not None:
            try:
                rows = self._parse_distribution_xlsx(dist_path)
                if rows:
                    self.db_arris.add_mvideo_distributions(rows)
                result["distribution"] = len(rows)
                self._info(f"distribution: записано строк {len(rows)}")
            except Exception as e:
                self._error(f"ошибка парсинга/записи distribution: {e}")

        # ACQUIRING
        acq_path = self._find_local_report(directory, all_xlsx, "acquiring")
        if acq_path is not None:
            try:
                rows = self._parse_acquiring_xlsx(acq_path)
                if rows:
                    self.db_arris.add_mvideo_acquirings(rows)
                result["acquiring"] = len(rows)
                self._info(f"acquiring: записано строк {len(rows)}")
            except Exception as e:
                self._error(f"ошибка парсинга/записи acquiring: {e}")

        # STORAGE
        stor_path = self._find_local_report(directory, all_xlsx, "storage")
        if stor_path is not None:
            try:
                rows = self._parse_storage_xlsx(stor_path)
                if rows:
                    self.db_arris.add_mvideo_stocks(rows)
                result["storage"] = len(rows)
                self._info(f"storage: записано строк {len(rows)}")
            except Exception as e:
                self._error(f"ошибка парсинга/записи storage: {e}")

        return result

    def _find_local_report(
            self,
            directory: str,
            all_xlsx: list[str],
            report_type: str,
    ) -> str | None:
        """
        Ищет в `all_xlsx` файл, имя которого содержит одну из подстрок
        из _LOCAL_REPORT_PATTERNS[report_type] (case-insensitive).
        Возвращает полный путь к первому совпавшему файлу или None.
        """
        patterns = self._LOCAL_REPORT_PATTERNS.get(report_type, ())
        matches: list[str] = []
        for name in all_xlsx:
            lower_name = name.lower()
            if any(p.lower() in lower_name for p in patterns):
                matches.append(name)

        if not matches:
            self._info(
                f"{report_type}: не найден файл с подстрокой "
                f"{patterns} в {directory}, пропускаю"
            )
            return None

        chosen = matches[0]
        if len(matches) > 1:
            self._info(
                f"{report_type}: найдено несколько файлов, беру первый '{chosen}', "
                f"остальные игнорирую: {matches[1:]}"
            )
        else:
            self._info(f"{report_type}: найден файл '{chosen}'")

        return os.path.join(directory, chosen)

    def _error_global(self, msg: str) -> None:
        """Лог ошибки без префикса company (когда market ещё не задан)."""
        logger.error(msg)

    # === вспомогательные HTTP-методы ===

    def _http_get_json(
            self,
            session: requests.Session,
            url: str,
            params: dict | None = None,
    ) -> dict | list | None:
        try:
            r = session.get(url, params=params, timeout=60)
            self._info(f"GET {r.url} status={r.status_code}")
            if r.status_code != 200:
                self._error(f"ошибка API {url} {r.status_code}: {r.text[:500]}")
                return None
            return r.json()
        except requests.RequestException as e:
            self._error(f"ошибка запроса {url}: {e}")
            return None
        except ValueError:
            self._error(f"API {url} вернул не JSON")
            return None

    def _http_post(
            self,
            session: requests.Session,
            url: str,
            params: dict | None = None,
            json_body: dict | None = None,
    ) -> bool:
        try:
            r = session.post(url, params=params, json=json_body or {}, timeout=60)
            self._info(f"POST {r.url} status={r.status_code}")
            if r.status_code not in (200, 201, 202, 204):
                self._error(f"ошибка API {url} {r.status_code}: {r.text[:500]}")
                return False
            return True
        except requests.RequestException as e:
            self._error(f"ошибка запроса {url}: {e}")
            return False

    def _http_post_json(
            self,
            session: requests.Session,
            url: str,
            params: dict | None = None,
            json_body: dict | None = None,
            content_type: str = "application/json",
    ) -> dict | list | None:
        try:
            r = session.post(
                url,
                params=params,
                data=json.dumps(json_body or {}),
                headers={"Content-Type": content_type},
                timeout=60,
            )
            self._info(f"POST {r.url} status={r.status_code}")
            if r.status_code != 200:
                self._error(f"ошибка API {url} {r.status_code}: {r.text[:500]}")
                return None
            return r.json()
        except requests.RequestException as e:
            self._error(f"ошибка запроса {url}: {e}")
            return None
        except ValueError:
            self._error(f"API {url} вернул не JSON")
            return None

    # === поллинг и матчинг ===

    def _poll_all_tasks(
            self,
            session: requests.Session,
            start_date: str,
            request_time: datetime,
            poll_interval_s: int,
            max_wait_s: int,
    ) -> dict[str, dict | None]:
        """
        Опрашивает /api/report/task/search до тех пор, пока для каждого из 4 типов
        не появится статус FINISH (вернёт сам task) или NO_DATA (None).
        Возвращает dict service_type -> task (или None если NO_DATA/таймаут).
        """
        url = f"{self.BASE}/api/report/task/search"
        search_body = {
            "pageable": {
                "page": 1,
                "size": 50,
                "sorts": [{"field": "createdTime", "sortType": "DESC"}],
            },
            "filters": [],
        }
        deadline = time.time() + max_wait_s

        # Маппинг taskType -> serviceType (то что ещё ждём)
        pending: dict[str, str] = {tt: st for st, tt, _ in SERVICE_TYPES}
        results: dict[str, dict | None] = {}

        while pending and time.time() < deadline:
            data = self._http_post_json(
                session,
                url,
                json_body=search_body,
                content_type="application/mvideo.api.v1+json",
            )

            if data is not None:
                for task in data.get("content", []):
                    task_type = task.get("taskType")
                    if task_type not in pending:
                        continue
                    if not self._task_matches(task, start_date, request_time):
                        continue

                    status = task.get("status")
                    service_type = pending[task_type]

                    if status == "FINISH":
                        results[service_type] = task
                        self._info(
                            f"  ✓ {service_type}: готов (task_id={task.get('id')})"
                        )
                        pending.pop(task_type, None)
                    elif status == "NO_DATA":
                        results[service_type] = None
                        self._info(f"  ⊘ {service_type}: NO_DATA, пропускаю")
                        pending.pop(task_type, None)
                    # PROCESSING/IN_PROGRESS/прочее — продолжаем ждать

            if pending:
                still_waiting = list(pending.values())
                self._info(
                    f"Ждём ещё {poll_interval_s}с (в работе: {still_waiting})"
                )
                time.sleep(poll_interval_s)

        # То что не дождались — фиксируем как None
        for tt, st in pending.items():
            results[st] = None
            self._error(f"  ✗ {st}: не сформировался за {max_wait_s}с")

        return results

    def _task_matches(self, task: dict, start_date: str, request_time: datetime) -> bool:
        """
        Проверяет, что task — наш свежий:
        - supplierCode == client_id
        - filter.date == start_date
        - createdTime (UTC) + 3h >= request_time (MSK)
        """
        if task.get("supplierCode") != self.market.client_id:
            return False

        movement = task.get("movementDetail") or {}
        try:
            filt = json.loads(movement.get("filter") or "{}")
        except ValueError:
            return False
        if filt.get("date") != start_date:
            return False

        created_str = task.get("createdTime")
        if not created_str:
            return False
        try:
            created_utc = datetime.fromisoformat(created_str)
        except ValueError:
            return False
        created_msk = created_utc + timedelta(hours=3)
        if created_msk < request_time:
            return False

        return True

    # === скачивание ===

    def _download_task_file(self, session: requests.Session, task: dict, download_dir: str) -> str | None:
        """
        Скачивает файл отчёта по task'у.
        Имя файла берётся из поля task['name'] (например
        'Отчет за услуги прямой дистрибуции K000073787 за 05_2026') + '.xlsx'.
        """
        task_id = task.get("id")
        if task_id is None:
            self._error("в task нет id")
            return None

        url = f"{self.BASE}/api/report/task/report/{task_id}"
        try:
            r = session.get(url, timeout=120)
            self._info(f"GET {r.url} status={r.status_code}")
            if r.status_code != 200:
                self._error(f"ошибка скачивания {r.status_code}: {r.text[:500]}")
                return None
        except requests.RequestException as e:
            self._error(f"ошибка скачивания: {e}")
            return None

        # Имя из name + расширение
        raw_name = (task.get("name") or f"report_{task_id}").strip()
        file_name = self._sanitize_filename(raw_name)
        if not file_name.lower().endswith(".xlsx"):
            file_name += ".xlsx"

        today_str = get_moscow_time().strftime("%Y-%m-%d")
        target_dir = os.path.join(
            download_dir,
            self.market.name_company,
            today_str,
        )
        os.makedirs(target_dir, exist_ok=True)

        save_path = os.path.join(target_dir, file_name)
        with open(save_path, "wb") as f:
            f.write(r.content)

        self._info(f"отчёт сохранён: {save_path}")
        return save_path

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Заменяет запрещённые в имени файла символы Windows на подчёркивания."""
        return re.sub(r'[<>:"/\\|?*]', "_", name).strip()

    # === парсинг distribution.xlsx ===

    # Имя листа в xlsx, на котором лежат данные
    DETAIL_SHEET_NAME = "Детализированный отчет"

    # Distribution (сводный отчёт): поле → точное название колонки в xlsx
    _COL_NAMES_DISTRIB: dict[str, str] = {
        "sku":         "Артикул Мвидео",
        "date":        "Дата чека",
        "tariff_rate": "Ставка по тарифу (руб., с учетом НДС)",
        "quantity":    "Количество доставленных единиц",
        "cost":        "Сумма к оплате с учетом тарификации (руб., с учетом НДС)",
    }

    # Acquiring (детализация по чекам): поле → точное название колонки в xlsx
    _COL_NAMES_ACQUIRING: dict[str, str] = {
        "sku":              "Артикул Мвидео",
        "date":             "Дата чека",
        "quantity":         "Количество единиц",
        "sum":              "Стоимость товара, руб. с учетом НДС",
        "total_sum":        "Сумма по товарам, руб. с учетом НДС",
        "transaction_type": "Тип транзакции",
        "cost":             "Сумма к оплате с учетом тарификации, руб. с учетом НДС",
    }


    def _parse_distribution_xlsx(self, file_path: str) -> list[DataMvideoDistribution]:
        """
        Парсит лист 'Детализированный отчет' в xlsx-отчёте распределения
        через pandas + calamine (терпим к битым стилям).
        """
        df = self._read_detail_sheet(file_path, label="distribution")
        if df is None:
            return []

        header_row, col_map = self._find_headers_in_df(
            df, self._COL_NAMES_DISTRIB, required_fields=("sku", "date"),
        )
        if header_row is None:
            self._error(
                f"distribution: на листе '{self.DETAIL_SHEET_NAME}' "
                f"не найдены заголовки (минимум sku + date)"
            )
            return []

        self._log_missing_columns("distribution", self._COL_NAMES_DISTRIB, col_map)
        self._info(
            f"distribution: лист '{self.DETAIL_SHEET_NAME}', "
            f"заголовки на строке {header_row + 1}, "
            f"найдено колонок: {len(col_map)}/{len(self._COL_NAMES_DISTRIB)}"
        )

        rows_data: list[DataMvideoDistribution] = []

        for row_idx in range(header_row + 1, len(df)):
            row = df.iloc[row_idx]

            def cell(field: str):
                return self._cell_from_row(row, col_map.get(field))

            sku_val = cell("sku")
            if sku_val is None:
                continue
            sku = str(sku_val).strip()
            if not sku:
                continue

            row_date = self._to_date(cell("date"))
            if row_date is None:
                continue

            rows_data.append(DataMvideoDistribution(
                date=row_date,
                client_id=self.market.client_id,
                sku=sku,
                tariff_rate=self._to_float(cell("tariff_rate")),
                quantity=self._to_int(cell("quantity")),
                cost=self._to_float(cell("cost")),
            ))

        self._info(f"distribution: распарсено строк: {len(rows_data)}")
        return rows_data

    def _parse_acquiring_xlsx(self, file_path: str) -> list[DataMvideoAcquiring]:
        """
        Парсит лист 'Детализированный отчет' в xlsx-отчёте acquiring
        через pandas + calamine.
        """
        df = self._read_detail_sheet(file_path, label="acquiring")
        if df is None:
            return []

        header_row, col_map = self._find_headers_in_df(
            df, self._COL_NAMES_ACQUIRING, required_fields=("sku", "date"),
        )
        if header_row is None:
            self._error(
                f"acquiring: на листе '{self.DETAIL_SHEET_NAME}' "
                f"не найдены заголовки (минимум sku + date)"
            )
            return []

        self._log_missing_columns("acquiring", self._COL_NAMES_ACQUIRING, col_map)
        self._info(
            f"acquiring: лист '{self.DETAIL_SHEET_NAME}', "
            f"заголовки на строке {header_row + 1}, "
            f"найдено колонок: {len(col_map)}/{len(self._COL_NAMES_ACQUIRING)}"
        )

        rows_data: list[DataMvideoAcquiring] = []

        for row_idx in range(header_row + 1, len(df)):
            row = df.iloc[row_idx]

            def cell(field: str):
                return self._cell_from_row(row, col_map.get(field))

            sku_val = cell("sku")
            if sku_val is None:
                continue
            sku = str(sku_val).strip()
            if not sku:
                continue

            row_date = self._to_date(cell("date"))
            if row_date is None:
                continue

            rows_data.append(DataMvideoAcquiring(
                date=row_date,
                client_id=self.market.client_id,
                sku=sku,
                quantity=self._to_int(cell("quantity")),
                sum=self._to_float(cell("sum")),
                total_sum=self._to_float(cell("total_sum")),
                transaction_type=self._to_str(cell("transaction_type")),
                cost=self._to_float(cell("cost")),
            ))

        self._info(f"acquiring: распарсено строк: {len(rows_data)}")
        return rows_data

    def _parse_storage_xlsx(self, file_path: str) -> list[DataMvideoStock]:
        """
        Парсит xlsx 'Услуга хранения' с ежедневными остатками.

        Структура отчёта:
            - 'Код поставщика'        → client_id
            - 'Код товара'            → sku
            - 'Макрорегион'           → 'С032 - Ростов-на-Дону' → warehouse=С032, city=Ростов-на-Дону
            - даты в заголовках       → 01.май, 02.май, ... (ячейка содержит datetime)
            - значение на пересечении → quantity_warehouse

        Каждая пара (SKU × день) с ненулевым количеством → одна строка в mv_stocks.
        Читает лист 'Детализированный отчет'.
        """
        df = self._read_detail_sheet(file_path, label="storage")
        if df is None:
            return []

        header_row, fixed_cols, date_cols = self._find_storage_headers(df)
        if header_row is None or "sku" not in fixed_cols:
            self._error(
                "storage: не найдена строка заголовков "
                "(нужны минимум 'Код товара' и хотя бы одна дата)"
            )
            return []

        self._info(
            f"storage: заголовки на строке {header_row + 1}, "
            f"дат: {len(date_cols)}, фикс. колонок: {sorted(fixed_cols)}"
        )

        rows_data: list[DataMvideoStock] = []

        for row_idx in range(header_row + 1, len(df)):
            row = df.iloc[row_idx]

            sku_val = self._cell_from_row(row, fixed_cols.get("sku"))
            if sku_val is None:
                continue
            sku = str(sku_val).strip()
            if not sku:
                continue

            # client_id — из колонки 'Код поставщика', либо из self.market как fallback
            client_id_val = self._cell_from_row(row, fixed_cols.get("client_id"))
            if client_id_val is not None and str(client_id_val).strip():
                client_id = str(client_id_val).strip()
            else:
                client_id = self.market.client_id

            macroregion_val = self._cell_from_row(row, fixed_cols.get("macroregion"))
            warehouse, city = self._split_macroregion(macroregion_val)

            tariff_rate = self._to_float(
                self._cell_from_row(row, fixed_cols.get("tariff_rate"))
            )

            for d, col_idx in date_cols.items():
                qty = self._to_int(self._cell_from_row(row, col_idx))
                if qty is None or qty == 0:
                    continue

                cost = qty * tariff_rate if tariff_rate is not None else None

                rows_data.append(DataMvideoStock(
                    date=d,
                    client_id=client_id,
                    sku=sku,
                    warehouse=warehouse,
                    city=city,
                    quantity_warehouse=qty,
                    cost=cost,
                ))

        self._info(f"storage: распарсено строк: {len(rows_data)}")
        return rows_data

    def _find_storage_headers(self, df) -> tuple[int | None, dict[str, int], dict[date, int]]:
        """
        Ищет строку заголовков отчёта 'Услуга хранения'.

        Возвращает кортеж (row_idx, fixed_cols, date_cols), где:
            fixed_cols = {'client_id': idx, 'sku': idx, 'macroregion': idx}
            date_cols  = {date(2026, 5, 1): idx, date(2026, 5, 2): idx, ...}

        Строка считается заголовочной, если в ней найден хотя бы 'Код товара'.
        """
        needed = {
            "Код поставщика": "client_id",
            "Код товара":     "sku",
            "Макрорегион":    "macroregion",
            "Ставка по тарифу за 1 штуку товара в день, руб., с учетом НДС": "tariff_rate",
        }
        needed_norm = {self._normalize_header(k): v for k, v in needed.items()}

        max_scan = min(30, len(df))
        for row_idx in range(max_scan):
            row = df.iloc[row_idx]
            fixed_cols: dict[str, int] = {}
            date_cols: dict[date, int] = {}
            for col_idx in range(len(row)):
                cell = row.iloc[col_idx]
                # Сначала пробуем как дату
                d = self._try_extract_date(cell)
                if d is not None:
                    if d not in date_cols:
                        date_cols[d] = col_idx
                    continue
                # Затем как фиксированное имя
                if isinstance(cell, str):
                    norm = self._normalize_header(cell)
                    field = needed_norm.get(norm)
                    if field is not None and field not in fixed_cols:
                        fixed_cols[field] = col_idx
            if "sku" in fixed_cols and date_cols:
                return row_idx, fixed_cols, date_cols
        return None, {}, {}

    @staticmethod
    def _try_extract_date(value) -> date | None:
        """Пытается извлечь date из ячейки (Timestamp / datetime / date / 'DD.MM.YYYY')."""
        if value is None:
            return None
        # pandas Timestamp
        if hasattr(value, "to_pydatetime"):
            try:
                return value.to_pydatetime().date()
            except Exception:
                pass
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            s = value.strip()
            try:
                return datetime.strptime(s, "%d.%m.%Y").date()
            except ValueError:
                pass
        return None

    @staticmethod
    def _split_macroregion(value) -> tuple[str | None, str | None]:
        """
        Делит строку макрорегиона вида 'С032 - Ростов-на-Дону' на (warehouse, city).
        Возвращает (None, None) если значение пустое/невалидное.
        Если разделителя нет — кладёт всю строку в warehouse, city=None.
        """
        if value is None:
            return None, None
        text = str(value).strip()
        if not text:
            return None, None
        # Делим максимум на 2 части по первому дефису
        # (название города может содержать дефис, например 'Ростов-на-Дону')
        parts = [p.strip() for p in text.split("-", 1)]
        if len(parts) == 2:
            warehouse = parts[0] or None
            city = parts[1] or None
            return warehouse, city
        return text, None

    def _read_detail_sheet(self, file_path: str, label: str):
        """
        Открывает xlsx через pandas+calamine и возвращает DataFrame листа
        DETAIL_SHEET_NAME без заголовка (header=None), чтобы заголовочную
        строку искать вручную по тексту. None при ошибке.
        """
        try:
            xls = pd.ExcelFile(file_path, engine="calamine")
        except Exception as e:
            self._error(f"{label}: не удалось открыть xlsx: {e}")
            return None

        target = self._normalize_header(self.DETAIL_SHEET_NAME)
        sheet_match = next(
            (s for s in xls.sheet_names if self._normalize_header(s) == target),
            None,
        )
        if sheet_match is None:
            self._error(
                f"{label}: лист '{self.DETAIL_SHEET_NAME}' не найден "
                f"(есть листы: {xls.sheet_names})"
            )
            return None

        try:
            df = xls.parse(sheet_match, header=None)
        except Exception as e:
            self._error(f"{label}: не удалось прочитать лист '{sheet_match}': {e}")
            return None

        return df

    def _find_headers_in_df(self, df, col_names: dict[str, str], required_fields: tuple[str, ...] = ("sku", "date_check"),
    ) -> tuple[int | None, dict[str, int]]:
        """
        Сканирует первые 30 строк DataFrame, ищет ту, где находятся точные
        названия колонок (см. col_names). Возвращает (индекс строки, col_map).
        Заголовки считаются найденными только если присутствуют все required_fields.
        """
        expected = {
            field: self._normalize_header(name)
            for field, name in col_names.items()
        }
        name_to_field = {norm: field for field, norm in expected.items()}

        best_row: int | None = None
        best_map: dict[str, int] = {}

        max_scan = min(30, len(df))
        for row_idx in range(max_scan):
            current: dict[str, int] = {}
            row = df.iloc[row_idx]
            for col_idx in range(len(row)):
                cell = row.iloc[col_idx]
                if not isinstance(cell, str):
                    continue
                norm = self._normalize_header(cell)
                field = name_to_field.get(norm)
                if field is not None and field not in current:
                    current[field] = col_idx

            if len(current) > len(best_map):
                best_map = current
                best_row = row_idx
                if len(best_map) == len(col_names):
                    break

        if all(req in best_map for req in required_fields):
            return best_row, best_map
        return None, {}

    @staticmethod
    def _cell_from_row(row, idx):
        """Безопасное извлечение ячейки pandas Series по индексу."""
        if idx is None or idx >= len(row):
            return None
        val = row.iloc[idx]
        if pd.isna(val):
            return None
        return val

    def _log_missing_columns(self, label: str, col_names: dict[str, str], found: dict[str, int]) -> None:
        """Пишет warning для каждой ожидаемой колонки, которой не нашлось."""
        missing = set(col_names.keys()) - set(found.keys())
        for field in missing:
            self._error(
                f"{label}: колонка '{col_names[field]}' не найдена "
                f"(возможно, название изменилось; ожидалось поле '{field}')"
            )

    @staticmethod
    def _normalize_header(value: str) -> str:
        """Приводит заголовок к каноничному виду для сравнения."""
        return re.sub(r"\s+", " ", value).strip().lower()

    @staticmethod
    def _to_str(v) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @staticmethod
    def _to_int(v) -> int | None:
        if v is None or v == "":
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_float(v) -> float | None:
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_date(v) -> date | None:
        if v is None:
            return None
        if pd.isna(v):
            return None
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        # Через pandas — терпимо к Timestamp, numpy.datetime64,
        # строкам "2026-05-11", "11.05.2026", "11/05/2026" и т.п.
        try:
            ts = pd.to_datetime(v, dayfirst=True, errors="raise")
        except (ValueError, TypeError):
            return None
        if pd.isna(ts):
            return None
        return ts.date() if hasattr(ts, "date") else None
