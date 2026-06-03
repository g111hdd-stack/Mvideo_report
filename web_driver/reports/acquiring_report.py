"""
Отчёт ACQUIRING — детализация по чекам (billing).
"""

from database.data_classes import DataMvideoAcquiring

from web_driver.reports._common import (
    BaseReport,
    DETAIL_SHEET_NAME,
    cell_from_row,
    to_date,
    to_float,
    to_int,
    to_str,
)


def _receipt_to_str(val) -> str | None:
    """
    Приводит «Номер чека» к строке.
    Excel хранит номер как число → pandas даёт float (1504106.0).
    Если это целое — отрезаем .0, чтобы в БД лёг '1504106', а не '1504106.0'.
    """
    if val is None:
        return None
    if isinstance(val, float):
        if val.is_integer():
            return str(int(val))
        return str(val)
    s = str(val).strip()
    return s or None


class AcquiringReport(BaseReport):
    """Парсит xlsx 'Эквайринг' и пишет в mv_acquiring."""

    SERVICE_TYPE = "ACQUIRING"
    TASK_TYPE = "BILLING_SUPPLIER_ACQUIRING"
    FILE_NAME = "acquiring.xlsx"
    LABEL = "acquiring"

    # Подстроки для поиска xlsx-файла в локальной папке (case-insensitive)
    PATTERNS: tuple[str, ...] = (
        "Эквайринг",
        "Проведению расчетов",
        "Проведение расчетов",
        "расчетов",
        "acquiring",
    )

    COL_NAMES: dict[str, str] = {
        "sku":              "Артикул Мвидео",
        "date":             "Дата чека",
        "receipt_number":   "Номер чека",
        "quantity":         "Количество единиц",
        "sum":              "Стоимость товара, руб. с учетом НДС",
        "total_sum":        "Сумма по товарам, руб. с учетом НДС",
        "transaction_type": "Тип транзакции",
        "cost":             "Сумма к оплате с учетом тарификации, руб. с учетом НДС",
    }

    def parse_xlsx(self, file_path: str) -> list[DataMvideoAcquiring]:
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

        rows_data: list[DataMvideoAcquiring] = []

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

            rows_data.append(DataMvideoAcquiring(
                date=row_date,
                client_id=self.market.client_id,
                sku=sku,
                receipt_number=_receipt_to_str(cell("receipt_number")),
                quantity=to_int(cell("quantity")),
                sum=to_float(cell("sum")),
                total_sum=to_float(cell("total_sum")),
                transaction_type=to_str(cell("transaction_type")),
                cost=to_float(cell("cost")),
            ))

        self._info(f"{self.LABEL}: распарсено строк: {len(rows_data)}")
        return rows_data

    def parse_and_save(self, file_path: str) -> int:
        try:
            rows = self.parse_xlsx(file_path)
        except Exception as e:
            self._error(f"ошибка парсинга {self.LABEL}: {e}")
            return 0
        if rows and self.db_arris is not None:
            try:
                self.db_arris.add_mvideo_acquirings(rows)
            except Exception as e:
                self._error(f"ошибка записи {self.LABEL} в БД: {e}")
                return 0
        return len(rows)
