"""
Отчёт DISTRIBUTION — сводный отчёт о распределении (billing).
"""

from database.data_classes import DataMvideoDistribution

from web_driver.reports._common import (
    BaseReport,
    DETAIL_SHEET_NAME,
    cell_from_row,
    to_date,
    to_float,
    to_int,
)


def _sum_optional(a, b):
    """Сумма с поддержкой None: None трактуется как 0, но (None + None) → None."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


class DistributionReport(BaseReport):
    """Парсит xlsx 'Распределение оплат' и пишет в mv_distribution."""

    SERVICE_TYPE = "DISTRIBUTION"
    TASK_TYPE = "BILLING_SUPPLIER_DISTRIBUTION"
    FILE_NAME = "distribution.xlsx"
    LABEL = "distribution"

    # Подстроки для поиска xlsx-файла в локальной папке (case-insensitive)
    PATTERNS: tuple[str, ...] = (
        "Распределение",
        "дистрибуции",
        "дистрибуция",
        "distribution",
    )

    # Имена колонок в xlsx (точные, до нормализации)
    COL_NAMES: dict[str, str] = {
        "sku":         "Артикул Мвидео",
        "date":        "Дата чека",
        "quantity":    "Количество доставленных единиц",
        "cost":        "Сумма к оплате с учетом тарификации (руб., с учетом НДС)",
    }

    def parse_xlsx(self, file_path: str) -> list[DataMvideoDistribution]:
        df = self._read_detail_sheet(file_path, label=self.LABEL)
        if df is None:
            return []

        header_row, col_map = self._find_headers_in_df(
            df, self.COL_NAMES, required_fields=("sku", "date"),
        )
        if header_row is None:
            self._error(
                f"{self.LABEL}: на листе '{DETAIL_SHEET_NAME}' "
                f"не найдены заголовки (минимум sku + date)"
            )
            return []

        self._log_missing_columns(self.LABEL, self.COL_NAMES, col_map)
        self._info(
            f"{self.LABEL}: лист '{DETAIL_SHEET_NAME}', "
            f"заголовки на строке {header_row + 1}, "
            f"найдено колонок: {len(col_map)}/{len(self.COL_NAMES)}"
        )

        rows_data: list[DataMvideoDistribution] = []

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

            row_date = to_date(cell("date"))
            if row_date is None:
                continue

            rows_data.append(DataMvideoDistribution(
                date=row_date,
                client_id=self.market.client_id,
                sku=sku,
                quantity=to_int(cell("quantity")),
                cost=to_float(cell("cost")),
            ))

        self._info(f"{self.LABEL}: распарсено строк: {len(rows_data)}")
        return rows_data

    def parse_and_save(self, file_path: str) -> int:
        """
        Парсит файл, агрегирует по (date, client_id, sku) с суммированием
        quantity и cost, и пишет в БД. Возвращает количество записанных строк.
        """
        try:
            rows = self.parse_xlsx(file_path)
        except Exception as e:
            self._error(f"ошибка парсинга {self.LABEL}: {e}")
            return 0
        if rows and self.db_arris is not None:
            rows = self._aggregate_rows(rows)
            try:
                self.db_arris.add_mvideo_distributions(rows)
            except Exception as e:
                self._error(f"ошибка записи {self.LABEL} в БД: {e}")
                return 0
        return len(rows)

    def _aggregate_rows(
            self,
            rows: list[DataMvideoDistribution],
    ) -> list[DataMvideoDistribution]:
        """
        Группирует строки по ключу (date, client_id, sku) и суммирует
        quantity и cost.

        None при суммировании трактуется как 0; если все слагаемые None — итог None.
        """
        grouped: dict[tuple, DataMvideoDistribution] = {}

        for r in rows:
            key = (r.date, r.client_id, r.sku)
            existing = grouped.get(key)
            if existing is None:
                grouped[key] = r
                continue

            existing.quantity = _sum_optional(existing.quantity, r.quantity)
            existing.cost = _sum_optional(existing.cost, r.cost)

        result = list(grouped.values())
        if len(result) != len(rows):
            self._info(
                f"{self.LABEL}: агрегация {len(rows)} → {len(result)} строк "
                f"(сумма quantity/cost по (date, client_id, sku))"
            )
        return result
