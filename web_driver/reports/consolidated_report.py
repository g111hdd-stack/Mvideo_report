"""
Консолидированный отчёт (Аналитика → Консолидированный отчёт).

Класс содержит только конфигурацию и парсинг xlsx 3P_main_report → mv_main_table.
Сетевая часть (триггер формирования, polling, скачивание) — в MvideoReports.
"""

from datetime import date as date_type

from database.data_classes import DataMvideoMainTable

from web_driver.reports._common import (
    BaseReport,
    cell_from_row,
    to_date,
    to_float,
    to_int,
    to_str,
)


class ConsolidatedReport(BaseReport):
    """Парсит 3P_main_report.xlsx и пишет в mv_main_table."""

    LABEL = "consolidated"
    FILE_NAME = "consolidated.xlsx"

    # Имя листа с данными в xlsx (переопределяем дефолт BaseReport)
    SHEET_NAME = "Отчёт по объектам"

    # Параметры API (используются MvideoReports для триггера и polling)
    REPORT_UUID = "6aa12950-6c01-451d-8c76-6725558374d8"
    TASK_NAME_PREFIX = "3P_main_report_"

    # Имена колонок в xlsx (точные, до нормализации)
    COL_NAMES: dict[str, str] = {
        "accrual_date":    "Дата",
        "delivery_schema": "Тип объекта",
        "sku":             "Код товара М.Видео",
        "sale":            "Продажи total, руб.",
        "quantities":      "Продажи total, шт.",
    }

    # ---------- Тело POST-запроса на формирование ----------

    @staticmethod
    def build_body(start_date: date_type, end_date: date_type) -> dict:
        """JSON-тело для POST /api/rd/partner/3p/report/{uuid}?saveOnly=false."""
        return {
            "reportType": "SUBSCRIPTION",
            "periodType": "RANGE",
            "period": "DAYS",
            "bindSupplier": True,
            "indicators": ["SELLS", "ONLINE"],
            "unitsForSells": ["RUB", "PIECES"],
            "channels": False,
            "trademark": ["MVIDEO", "ELDORADO"],
            "city": False,
            "plant": True,
            "dateRange": {
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate":   end_date.strftime("%Y-%m-%d"),
            },
            "productType": ["MARKETPLACE", "AGENCY_PRODUCTS"],
            "withDynamicsDashboard": False,
            "withTopSalesDashboard": False,
            "withPromoDashboard": False,
            "withShareSalesDashboard": False,
            "withExcelReportGenerating": False,
            "withFilterByProduct": False,
            "withFilterByPlant": False,
            "withResults": False,
            "allHierarchySelected": True,
        }

    # ---------- Парсинг xlsx ----------

    def parse_xlsx(self, file_path: str) -> list[DataMvideoMainTable]:
        """
        Парсит лист SHEET_NAME в xlsx 3P_main_report.
        Каждая строка с непустыми sku/date/sale → одна запись DataMvideoMainTable.
        type_of_transaction выводится из знака sale:
            sale > 0 → 'delivered'
            sale < 0 → 'cancelled'
        """
        df = self._read_detail_sheet(file_path, label=self.LABEL)
        if df is None:
            return []

        header_row, col_map = self._find_headers_in_df(
            df, self.COL_NAMES, required_fields=("accrual_date", "sku"),
        )
        if header_row is None:
            self._error(
                f"{self.LABEL}: не найдены заголовки "
                f"(минимум 'Дата' + 'Код товара М.Видео')"
            )
            return []

        self._log_missing_columns(self.LABEL, self.COL_NAMES, col_map)
        self._info(
            f"{self.LABEL}: заголовки на строке {header_row + 1}, "
            f"найдено колонок: {len(col_map)}/{len(self.COL_NAMES)}"
        )

        rows_data: list[DataMvideoMainTable] = []

        for row_idx in range(header_row + 1, len(df)):
            row = df.iloc[row_idx]

            def cell(field: str):
                return cell_from_row(row, col_map.get(field))

            sku_val = cell("sku")
            if sku_val is None:
                continue
            sku = str(sku_val).strip()
            if not sku:
                continue

            row_date = to_date(cell("accrual_date"))
            if row_date is None:
                continue

            sale = to_float(cell("sale"))
            if sale is None or sale == 0:
                continue

            type_of_transaction = "delivered" if sale > 0 else "cancelled"

            rows_data.append(DataMvideoMainTable(
                accrual_date=row_date,
                client_id=self.market.client_id,
                type_of_transaction=type_of_transaction,
                sku=sku,
                delivery_schema=to_str(cell("delivery_schema")),
                vendor_code=None,
                sale=sale,
                quantities=to_int(cell("quantities")),
                commission=None,
            ))

        self._info(f"{self.LABEL}: распарсено строк: {len(rows_data)}")
        return rows_data

    def parse_and_save(self, file_path: str) -> int:
        """
        Парсит файл, обогащает строки vendor_code + commission из mv_card_product
        и пишет в mv_main_table. Возвращает количество записанных строк.
        """
        try:
            rows = self.parse_xlsx(file_path)
        except Exception as e:
            self._error(f"ошибка парсинга {self.LABEL}: {e}")
            return 0

        if not rows:
            return 0

        if self.db_arris is not None:
            self._enrich_from_card_products(rows)
            try:
                self.db_arris.add_mvideo_main_tables(rows)
            except Exception as e:
                self._error(f"ошибка записи {self.LABEL} в БД: {e}")
                return 0

        return len(rows)

    def _enrich_from_card_products(self, rows: list[DataMvideoMainTable]) -> None:
        """
        Подтягивает vendor_code и считает commission = sale * commission_rate
        по данным из mv_card_product. Модифицирует строки на месте.
        Если SKU нет в mv_card_product — оба поля останутся None.
        """
        skus = list({r.sku for r in rows})
        try:
            meta = self.db_arris.get_card_products_meta(
                client_id=self.market.client_id,
                skus=skus,
            )
        except Exception as e:
            self._error(f"{self.LABEL}: ошибка чтения mv_card_product: {e}")
            return

        missing: list[str] = []
        for r in rows:
            info = meta.get(r.sku)
            if info is None:
                missing.append(r.sku)
                continue
            vendor_code, commission_rate = info
            r.vendor_code = vendor_code
            if commission_rate is not None and r.sale is not None:
                r.commission = round(r.sale * commission_rate, 2)

        if missing:
            unique_missing = sorted(set(missing))
            self._info(
                f"{self.LABEL}: для {len(unique_missing)} SKU нет записи в "
                f"mv_card_product — vendor_code/commission останутся NULL "
                f"(пример: {unique_missing[:5]})"
            )
