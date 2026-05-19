"""
MvideoReports — координатор отчётов МВидео.

Делает три вещи:
1. Скачивает billing-отчёты (DISTRIBUTION/STORAGE/ACQUIRING/COMMISSION) — общий
   многошаговый flow: POST на формирование → polling task/search → GET готового xlsx.
2. Скачивает консолидированный отчёт (analytics — POST + polling + GET).
3. Парсит локальную папку с уже скачанными xlsx и пишет в БД.

Сами парсеры и логика per-report — в web_driver/reports/*.py.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, date as date_type

import requests

from log_api.log import logger, get_moscow_time

from web_driver.reports import (
    DistributionReport,
    AcquiringReport,
    StorageReport,
    ConsolidatedReport,
)


# (serviceType, taskType, имя файла) — порядок важен для logging и итерации
SERVICE_TYPES: list[tuple[str, str, str]] = [
    ("DISTRIBUTION", "BILLING_SUPPLIER_DISTRIBUTION", "distribution.xlsx"),
    ("STORAGE",      "BILLING_SUPPLIER_STORAGE",      "storage.xlsx"),
    ("ACQUIRING",    "BILLING_SUPPLIER_ACQUIRING",    "acquiring.xlsx"),
    ("COMMISSION",   "BILLING_SUPPLIER_COMMISSION",   "commission.xlsx"),
]


class MvideoReports:
    """HTTP-клиент для отчётов МВидео + координатор per-report парсеров."""

    BASE = "https://sellers.mvideo.ru"

    # Список классов отчётов, для которых есть парсинг billing-xlsx → БД
    BILLING_REPORT_CLASSES = [DistributionReport, AcquiringReport, StorageReport]

    def __init__(self, driver=None, db_arris=None, market=None) -> None:
        """
        Если передан driver — берём page/context/market из него (онлайн-режим).
        Если driver=None — оффлайн-режим (только парсинг локальных файлов),
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

        # Per-report инстансы — создаются один раз и переиспользуются
        self._reports_by_service: dict[str, object] = {
            cls.SERVICE_TYPE: cls(market=self.market, db_arris=db_arris)
            for cls in self.BILLING_REPORT_CLASSES
        }

    # =====================================================================
    # Логирование
    # =====================================================================

    def _info(self, msg: str) -> None:
        prefix = f"{self.market.name_company}: " if self.market is not None else ""
        logger.info(f"{prefix}{msg}")

    def _error(self, msg: str) -> None:
        prefix = f"{self.market.name_company}: " if self.market is not None else ""
        logger.error(f"{prefix}{msg}")

    # =====================================================================
    # Токен и сессия
    # =====================================================================

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

    # =====================================================================
    # Скачивание billing-отчётов
    # =====================================================================

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
           a) POST на формирование (по одному запросу на каждый serviceType)
           b) Поллим /api/report/task/search, пока для каждого task'а
              не появится status='FINISH' (тогда скачиваем) или 'NO_DATA' (пропускаем)
           c) GET /api/report/task/report/{id} — скачиваем готовые xlsx
        Сохраняет файлы в report/<name_company>/<today_msk>/<service>.xlsx
        После скачивания парсит файл соответствующим отчётом и пишет в БД.
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

            # 3. Запускаем формирование всех типов
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

            # 4. Поллим все task'и одновременно
            results = self._poll_all_tasks(
                session=session,
                start_date=start_date,
                request_time=request_time,
                poll_interval_s=poll_interval_s,
                max_wait_s=max_wait_s,
            )

            # 5. Скачиваем готовые + парсим через per-report классы
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

                # Делегируем парсинг + запись соответствующему классу отчёта
                report = self._reports_by_service.get(service_type)
                if report is not None and self.db_arris is not None:
                    saved_rows = report.parse_and_save(path)
                    self._info(
                        f"{service_type}: записано строк {saved_rows}"
                    )

        return saved

    # =====================================================================
    # Скачивание консолидированного отчёта (одиночный task)
    # =====================================================================

    def download_consolidated_report(
            self,
            download_dir: str = "report",
            start_date: date_type | None = None,
            end_date: date_type | None = None,
            poll_interval_s: int = 15,
            max_wait_s: int = 600,
    ) -> str | None:
        """
        Полный flow: триггер формирования → polling одного task → скачивание xlsx → парсинг + БД.

        По умолчанию period = текущий месяц по MSK.
        Делегирует только parse_and_save в ConsolidatedReport; всю сеть обрабатывает сам.
        """
        today_msk = get_moscow_time().date()
        if start_date is None:
            start_date = today_msk.replace(day=1)
        if end_date is None:
            end_date = today_msk

        referer = f"{self.BASE}/mpa/analytics/reports"
        session = self._build_session(referer)
        if session is None:
            return None

        report = ConsolidatedReport(market=self.market, db_arris=self.db_arris)

        # 1. Триггер формирования
        request_time = get_moscow_time() - timedelta(minutes=1)
        body = report.build_body(start_date, end_date)
        self._info(
            f"{report.LABEL}: триггер за период "
            f"{body['dateRange']['startDate']} … {body['dateRange']['endDate']}"
        )
        url = f"{self.BASE}/api/rd/partner/3p/report/{report.REPORT_UUID}"
        try:
            r = session.post(
                url,
                params={"saveOnly": "false"},
                json=body,
                timeout=60,
            )
            self._info(f"POST {r.url} status={r.status_code}")
            if r.status_code != 200:
                self._error(
                    f"{report.LABEL}: триггер не прошёл, "
                    f"status={r.status_code}, body={r.text[:500]}"
                )
                return None
        except requests.RequestException as e:
            self._error(f"{report.LABEL}: ошибка триггера: {e}")
            return None

        # 2. Polling — ждём один task по имени и времени создания
        task = self._poll_single_task(
            session=session,
            match_fn=lambda t: self._consolidated_task_matches(
                t, report.TASK_NAME_PREFIX, request_time,
            ),
            label=report.LABEL,
            poll_interval_s=poll_interval_s,
            max_wait_s=max_wait_s,
        )
        if task is None:
            return None

        # 3. Скачивание готового xlsx
        path = self._download_task_file(
            session=session,
            task=task,
            download_dir=download_dir,
        )
        if path is None:
            return None

        # 4. Парсинг + запись в БД
        if self.db_arris is not None:
            saved_rows = report.parse_and_save(path)
            self._info(f"{report.LABEL}: записано строк {saved_rows}")

        return path

    def _poll_single_task(
            self,
            session: requests.Session,
            match_fn,
            label: str,
            poll_interval_s: int,
            max_wait_s: int,
    ) -> dict | None:
        """
        Поллит /api/report/task/search до тех пор, пока match_fn не найдёт нужный task.
        Возвращает task в статусе FINISH или None (NO_DATA / таймаут).
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

        while time.time() < deadline:
            data = self._http_post_json(
                session,
                url,
                json_body=search_body,
                content_type="application/mvideo.api.v1+json",
            )
            if data is not None:
                for task in data.get("content", []):
                    if not match_fn(task):
                        continue
                    status = task.get("status")
                    if status == "FINISH":
                        self._info(
                            f"{label}: ✓ готов "
                            f"(task_id={task.get('id')}, name={task.get('name')})"
                        )
                        return task
                    if status == "NO_DATA":
                        self._info(f"{label}: ⊘ NO_DATA, пропускаю")
                        return None
                    # PROCESSING/ADD/прочее — продолжаем ждать
                    self._info(
                        f"{label}: статус '{status}', ждём ещё {poll_interval_s}с"
                    )
                    break
            time.sleep(poll_interval_s)

        self._error(f"{label}: не сформировался за {max_wait_s}с")
        return None

    def _consolidated_task_matches(
            self,
            task: dict,
            name_prefix: str,
            request_time: datetime,
    ) -> bool:
        """
        Проверяет, что task — свежий консолидированный:
        - name начинается на name_prefix (например '3P_main_report_')
        - createdTime (UTC + 3h) >= request_time (MSK)
        """
        name = task.get("name") or ""
        if not name.startswith(name_prefix):
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

    # =====================================================================
    # Локальный парсинг уже скачанных файлов
    # =====================================================================

    def parse_local_directory(
            self,
            directory: str,
            period_date: date_type | None = None,
    ) -> dict[str, int]:
        """
        Парсит уже скачанные xlsx-отчёты из указанной папки и пишет их в БД.
        Для каждого зарегистрированного отчёта ищет xlsx по подстрокам PATTERNS
        и вызывает parse_and_save.

        Не требует driver/браузер — работает только с локальными файлами и БД.
        Возвращает dict { LABEL: количество_записанных_строк }.
        """
        if self.market is None:
            logger.error("parse_local_directory: market не задан")
            return {}

        if self.db_arris is None:
            self._error("parse_local_directory: db_arris не передан, запись в БД невозможна")
            return {}

        if not os.path.isdir(directory):
            self._error(f"parse_local_directory: папка не найдена: {directory}")
            return {}

        period_str = period_date.strftime("%Y-%m-%d") if period_date else "—"
        self._info(
            f"Локальный парсинг папки: {directory} (period_date={period_str})"
        )

        # Все xlsx-файлы в папке (один раз)
        all_xlsx = [
            f for f in os.listdir(directory)
            if f.lower().endswith(".xlsx") and os.path.isfile(os.path.join(directory, f))
        ]

        result: dict[str, int] = {}

        for report in self._reports_by_service.values():
            path = self._find_local_report(directory, all_xlsx, report)
            if path is None:
                result[report.LABEL] = 0
                continue
            saved_rows = report.parse_and_save(path)
            result[report.LABEL] = saved_rows
            self._info(f"{report.LABEL}: записано строк {saved_rows}")

        return result

    def _find_local_report(
            self,
            directory: str,
            all_xlsx: list[str],
            report,
    ) -> str | None:
        """
        Ищет в `all_xlsx` файл, имя которого содержит одну из подстрок
        report.PATTERNS (case-insensitive).
        Возвращает полный путь к первому совпавшему файлу или None.
        """
        patterns = report.PATTERNS
        matches: list[str] = []
        for name in all_xlsx:
            lower_name = name.lower()
            if any(p.lower() in lower_name for p in patterns):
                matches.append(name)

        if not matches:
            self._info(
                f"{report.LABEL}: не найден файл с подстрокой "
                f"{patterns} в {directory}, пропускаю"
            )
            return None

        chosen = matches[0]
        if len(matches) > 1:
            self._info(
                f"{report.LABEL}: найдено несколько файлов, беру первый '{chosen}', "
                f"остальные игнорирую: {matches[1:]}"
            )
        else:
            self._info(f"{report.LABEL}: найден файл '{chosen}'")

        return os.path.join(directory, chosen)

    # =====================================================================
    # Вспомогательные HTTP-методы
    # =====================================================================

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

    # =====================================================================
    # Поллинг билинг-задач
    # =====================================================================

    def _poll_all_tasks(
            self,
            session: requests.Session,
            start_date: str,
            request_time: datetime,
            poll_interval_s: int,
            max_wait_s: int,
    ) -> dict[str, dict | None]:
        """
        Опрашивает /api/report/task/search до тех пор, пока для каждого из типов
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

    # =====================================================================
    # Скачивание готового xlsx по task
    # =====================================================================

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
