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
from web_driver.reports._common import BaseReport


class MvideoReports:
    """HTTP-клиент для отчётов МВидео + координатор per-report парсеров."""

    BASE = "https://sellers.mvideo.ru"

    # Список классов отчётов, для которых есть парсинг billing-xlsx → БД
    BILLING_REPORT_CLASSES = [DistributionReport, AcquiringReport, StorageReport]

    # Статусы периодов, для которых нужно скачивать отчёты.
    PERIOD_STATUSES_TO_DOWNLOAD: tuple[str, ...] = ("OPEN", "ACCUMULATING")

    # Маппинг client_id -> supplierMpaId для запроса периодов STORAGE.
    # Числовой ID поставщика в MPA-системе (отличается от client_id вида K000xxxxx).
    # Где взять: DevTools → Network → ищем proxy/?...userId={mpa_id}_...
    SUPPLIER_MPA_IDS: dict[str, int] = {
        "K000071171": 10440,      # Бурчян Г.С.
        "K000073787": 13169,      # Лебедев М.С.
        "K000074004": 13474,      # Мкртчян Х.М.
    }

    def __init__(self, driver=None, db_arris=None, market=None) -> None:
        """
        Если передан driver — берём page/context/market из него (онлайн-режим).
        Если driver=None — оффлайн-режим (только парсинг локальных файлов),
        в этом случае нужно передать market отдельно.
        """
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
        self._reports_by_service: dict[str, BaseReport] = {
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
    # Публичные методы скачивания billing-отчётов (по сервису)
    # =====================================================================

    def download_distribution_reports(
            self,
            download_dir: str = "report",
            poll_interval_s: int = 30,
            max_wait_s: int = 360,
    ) -> list[str]:
        """Скачивает DISTRIBUTION-отчёты за все периоды OPEN/ACCUMULATING."""
        session = self._build_session(f"{self.BASE}/mpa/billing/reports/distribution")
        if session is None:
            return []
        periods = self._fetch_periods_distribution(session)
        return self._download_periods(
            session,
            periods=periods,
            service_type=DistributionReport.SERVICE_TYPE,
            task_type=DistributionReport.TASK_TYPE,
            label=DistributionReport.LABEL,
            download_dir=download_dir,
            poll_interval_s=poll_interval_s,
            max_wait_s=max_wait_s,
        )

    def download_storage_reports(
            self,
            download_dir: str = "report",
            poll_interval_s: int = 30,
            max_wait_s: int = 360,
    ) -> list[str]:
        """Скачивает STORAGE-отчёты за все периоды OPEN/ACCUMULATING."""
        session = self._build_session(f"{self.BASE}/mpa/billing/reports/storage")
        if session is None:
            return []
        periods = self._fetch_periods_storage(session)
        return self._download_periods(
            session,
            periods=periods,
            service_type=StorageReport.SERVICE_TYPE,
            task_type=StorageReport.TASK_TYPE,
            label=StorageReport.LABEL,
            download_dir=download_dir,
            poll_interval_s=poll_interval_s,
            max_wait_s=max_wait_s,
        )

    def download_acquiring_reports(
            self,
            download_dir: str = "report",
            poll_interval_s: int = 30,
            max_wait_s: int = 360,
    ) -> list[str]:
        """Скачивает ACQUIRING-отчёты за все периоды OPEN/ACCUMULATING."""
        session = self._build_session(f"{self.BASE}/mpa/billing/reports/acquiring")
        if session is None:
            return []
        periods = self._fetch_periods_acquiring(session)
        return self._download_periods(
            session,
            periods=periods,
            service_type=AcquiringReport.SERVICE_TYPE,
            task_type=AcquiringReport.TASK_TYPE,
            label=AcquiringReport.LABEL,
            download_dir=download_dir,
            poll_interval_s=poll_interval_s,
            max_wait_s=max_wait_s,
        )

    def download_commission_reports(
            self,
            download_dir: str = "report",
            poll_interval_s: int = 30,
            max_wait_s: int = 360,
    ) -> list[str]:
        """
        Скачивает COMMISSION-отчёты за все периоды OPEN/ACCUMULATING.
        Парсер не подключён — xlsx только сохраняется на диск.
        """
        session = self._build_session(f"{self.BASE}/mpa/billing/reports/commission")
        if session is None:
            return []
        periods = self._fetch_periods_commission(session)
        return self._download_periods(
            session,
            periods=periods,
            service_type="COMMISSION",
            task_type="BILLING_SUPPLIER_COMMISSION",
            label="commission",
            download_dir=download_dir,
            poll_interval_s=poll_interval_s,
            max_wait_s=max_wait_s,
        )

    def download_all_billing_reports(
            self,
            download_dir: str = "report",
            poll_interval_s: int = 30,
            max_wait_s: int = 360,
    ) -> dict[str, list[str]]:
        """Скачивает все billing-отчёты последовательно. Удобно вызывать из main."""
        kwargs = dict(
            download_dir=download_dir,
            poll_interval_s=poll_interval_s,
            max_wait_s=max_wait_s,
        )
        return {
            "distribution": self.download_distribution_reports(**kwargs),
            "storage":      self.download_storage_reports(**kwargs),
            "acquiring":    self.download_acquiring_reports(**kwargs),
            "commission":   self.download_commission_reports(**kwargs),
        }

    # =====================================================================
    # Запрос списков периодов (URL/формат у каждого сервиса свой)
    # =====================================================================

    def _fetch_periods_distribution(self, session: requests.Session) -> list[dict]:
        """GET /api/v1/billing/distribution/periods/supplier?page=0&size=50&sort="""
        data = self._http_get_json(
            session,
            f"{self.BASE}/api/v1/billing/distribution/periods/supplier",
            params={"page": 0, "size": 50, "sort": ""},
        )
        if not data:
            return []
        return data.get("content", []) if isinstance(data, dict) else []

    def _fetch_periods_storage(self, session: requests.Session) -> list[dict]:
        """POST /api/v1/billing/periods/storage/supplier?supplierMpaId=…"""
        mpa_id = self.SUPPLIER_MPA_IDS.get(self.market.client_id)
        if not mpa_id:
            self._error(
                f"STORAGE: supplierMpaId не задан для client_id={self.market.client_id} "
                f"(добавьте в MvideoReports.SUPPLIER_MPA_IDS)"
            )
            return []
        data = self._http_post_json(
            session,
            f"{self.BASE}/api/v1/billing/periods/storage/supplier",
            params={"supplierMpaId": mpa_id},
            json_body={
                "page": 1, "size": 50,
                "sorts": [{"field": "startDate", "sortType": "DESC"}],
            },
        )
        if not data:
            return []
        return data.get("content", []) if isinstance(data, dict) else []

    def _fetch_periods_acquiring(self, session: requests.Session) -> list[dict]:
        """POST /api/v1/billing/ACQUIRING/periods"""
        data = self._http_post_json(
            session,
            f"{self.BASE}/api/v1/billing/ACQUIRING/periods",
            json_body={
                "page": 1, "size": 50,
                "sorts": [{"field": "startDate", "sortType": "DESC"}],
            },
        )
        if not data:
            return []
        return data.get("content", []) if isinstance(data, dict) else []

    def _fetch_periods_commission(self, session: requests.Session) -> list[dict]:
        """POST /api/v1/billing/COMMISSION/periods"""
        data = self._http_post_json(
            session,
            f"{self.BASE}/api/v1/billing/COMMISSION/periods",
            json_body={
                "page": 1, "size": 50,
                "sorts": [{"field": "startDate", "sortType": "DESC"}],
            },
        )
        if not data:
            return []
        return data.get("content", []) if isinstance(data, dict) else []

    # =====================================================================
    # Общая логика скачивания всех периодов одного сервиса
    # =====================================================================

    def _download_periods(
            self,
            session: requests.Session,
            *,
            periods: list[dict],
            service_type: str,
            task_type: str,
            label: str,
            download_dir: str,
            poll_interval_s: int,
            max_wait_s: int,
    ) -> list[str]:
        """Фильтрует периоды и качает каждый по очереди через _download_one_billing_period."""
        if not periods:
            self._info(f"{label}: список периодов пуст")
            return []

        to_download = [
            p for p in periods
            if p.get("status") in self.PERIOD_STATUSES_TO_DOWNLOAD
        ]
        statuses_str = "/".join(self.PERIOD_STATUSES_TO_DOWNLOAD)
        self._info(f"{label}: найдено периодов {statuses_str}: {len(to_download)}")
        if not to_download:
            return []

        report = self._reports_by_service.get(service_type)

        saved: list[str] = []
        for period in to_download:
            start_date = period["startDate"]
            path = self._download_one_billing_period(
                session=session,
                service_type=service_type,
                task_type=task_type,
                label=label,
                start_date=start_date,
                report=report,
                download_dir=download_dir,
                poll_interval_s=poll_interval_s,
                max_wait_s=max_wait_s,
            )
            if path is not None:
                saved.append(path)
        return saved

    def _download_one_billing_period(
            self,
            session: requests.Session,
            *,
            service_type: str,
            task_type: str,
            label: str,
            start_date: str,
            report,
            download_dir: str,
            poll_interval_s: int,
            max_wait_s: int,
    ) -> str | None:
        """Один период одного сервиса: триггер POST → polling → скачивание → парсинг."""
        request_time = get_moscow_time() - timedelta(minutes=1)
        self._info(
            f"{label}: инициирую формирование за {start_date} "
            f"(request_time MSK: {request_time:%Y-%m-%d %H:%M:%S})"
        )

        ok = self._http_post(
            session,
            f"{self.BASE}/api/v1/rd/billing/report/supplier/{self.market.client_id}",
            params={
                "serviceType": service_type,
                "date": start_date,
                "closed": "false",
            },
        )
        if not ok:
            self._error(f"{label}: ошибка инициации за {start_date}")
            return None

        task = self._poll_single_task(
            session=session,
            match_fn=lambda t: (
                t.get("taskType") == task_type
                and self._task_matches(t, start_date, request_time)
            ),
            label=f"{label} ({start_date})",
            poll_interval_s=poll_interval_s,
            max_wait_s=max_wait_s,
        )
        if task is None:
            return None

        path = self._download_task_file(
            session=session, task=task, download_dir=download_dir,
        )
        if path is None:
            return None

        if report is not None and self.db_arris is not None:
            saved_rows = report.parse_and_save(path)
            self._info(f"{label}: записано строк {saved_rows}")

        return path

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
        # По умолчанию берём последние 10 дней включая сегодня.
        # Пример: сегодня 19.05.2026 → start=10.05.2026, end=19.05.2026 (10 дней).
        today_msk = get_moscow_time().date()
        if end_date is None:
            end_date = today_msk
        if start_date is None:
            start_date = today_msk - timedelta(days=9)

        referer = f"{self.BASE}/mpa/analytics/reports"
        session = self._build_session(referer)
        if session is None:
            return None

        report = ConsolidatedReport(market=self.market, db_arris=self.db_arris)

        # UUID отчёта зависит от кабинета (берём из карты внутри ConsolidatedReport)
        report_uuid = report.REPORT_UUIDS.get(self.market.client_id)
        if report_uuid is None:
            self._error(
                f"{report.LABEL}: нет REPORT_UUID для client_id={self.market.client_id}"
            )
            return None

        # 1. Триггер формирования
        request_time = get_moscow_time() - timedelta(minutes=1)
        body = report.build_body(start_date, end_date)
        self._info(
            f"{report.LABEL}: триггер за период "
            f"{body['dateRange']['startDate']} … {body['dateRange']['endDate']}"
        )
        url = f"{self.BASE}/api/rd/partner/3p/report/{report_uuid}"
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
    # Матчинг billing-задач (используется из _download_one_billing_period)
    # =====================================================================

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
