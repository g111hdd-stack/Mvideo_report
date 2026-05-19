"""
Отчёт STORAGE — услуга хранения с ежедневными остатками (billing).

Структура отчёта (лист 'Детализированный отчет'):
    - 'Код поставщика'        → client_id
    - 'Код товара'            → sku
    - 'Макрорегион'           → 'С032 - Ростов-на-Дону' → warehouse=С032, city=Ростов-на-Дону
    - даты в заголовках       → 01.май, 02.май, ... (ячейка содержит datetime)
    - значение на пересечении → quantity_warehouse
    - 'Ставка по тарифу...'   → tariff_rate (одна на строку)

cost = quantity_warehouse * tariff_rate
"""

from datetime import date as date_type

from database.data_classes import DataMvideoStock

from web_driver.reports._common import (
    BaseReport,
    cell_from_row,
    normalize_header,
    split_macroregion,
    to_float,
    to_int,
    try_extract_date,
)


class StorageReport(BaseReport):
    """Парсит xlsx 'Услуга хранения' и пишет в mv_stocks."""

    SERVICE_TYPE = "STORAGE"
    TASK_TYPE = "BILLING_SUPPLIER_STORAGE"
    FILE_NAME = "storage.xlsx"
    LABEL = "storage"

    PATTERNS: tuple[str, ...] = (
        "Услуга хранения",
        "Хранение",
        "storage",
    )

    # Имена «фиксированных» колонок (есть в каждой строке, не зависят от дат)
    FIXED_COL_NAMES: dict[str, str] = {
        "Код поставщика": "client_id",
        "Код товара":     "sku",
        "Макрорегион":    "macroregion",
        "Ставка по тарифу за 1 штуку товара в день, руб., с учетом НДС": "tariff_rate",
    }

    def parse_xlsx(self, file_path: str) -> list[DataMvideoStock]:
        df = self._read_detail_sheet(file_path, label=self.LABEL)
        if df is None:
            return []

        header_row, fixed_cols, date_cols = self._find_storage_headers(df)
        if header_row is None or "sku" not in fixed_cols:
            self._error(
                f"{self.LABEL}: не найдена строка заголовков "
                f"(нужны минимум 'Код товара' и хотя бы одна дата)"
            )
            return []

        self._info(
            f"{self.LABEL}: заголовки на строке {header_row + 1}, "
            f"дат: {len(date_cols)}, фикс. колонок: {sorted(fixed_cols)}"
        )

        rows_data: list[DataMvideoStock] = []

        for row_idx in range(header_row + 1, len(df)):
            row = df.iloc[row_idx]

            sku_val = cell_from_row(row, fixed_cols.get("sku"))
            if sku_val is None:
                continue
            sku = str(sku_val).strip()
            if not sku:
                continue

            # client_id — из колонки 'Код поставщика', либо из self.market как fallback
            client_id_val = cell_from_row(row, fixed_cols.get("client_id"))
            if client_id_val is not None and str(client_id_val).strip():
                client_id = str(client_id_val).strip()
            else:
                client_id = self.market.client_id

            macroregion_val = cell_from_row(row, fixed_cols.get("macroregion"))
            warehouse, city = split_macroregion(macroregion_val)

            tariff_rate = to_float(
                cell_from_row(row, fixed_cols.get("tariff_rate"))
            )

            for d, col_idx in date_cols.items():
                qty = to_int(cell_from_row(row, col_idx))
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
                self.db_arris.add_mvideo_stocks(rows)
            except Exception as e:
                self._error(f"ошибка записи {self.LABEL} в БД: {e}")
                return 0
        return len(rows)

    def _find_storage_headers(self, df) -> tuple[int | None, dict[str, int], dict[date_type, int]]:
        """
        Ищет строку заголовков отчёта 'Услуга хранения'.

        Возвращает кортеж (row_idx, fixed_cols, date_cols), где:
            fixed_cols = {'client_id': idx, 'sku': idx, 'macroregion': idx, 'tariff_rate': idx}
            date_cols  = {date(2026, 5, 1): idx, date(2026, 5, 2): idx, ...}

        Строка считается заголовочной, если в ней найден хотя бы 'Код товара' + дата.
        """
        needed_norm = {
            normalize_header(k): v for k, v in self.FIXED_COL_NAMES.items()
        }

        max_scan = min(30, len(df))
        for row_idx in range(max_scan):
            row = df.iloc[row_idx]
            fixed_cols: dict[str, int] = {}
            date_cols: dict[date_type, int] = {}
            for col_idx in range(len(row)):
                cell = row.iloc[col_idx]
                # Сначала пробуем как дату
                d = try_extract_date(cell)
                if d is not None:
                    if d not in date_cols:
                        date_cols[d] = col_idx
                    continue
                # Затем как фиксированное имя
                if isinstance(cell, str):
                    norm = normalize_header(cell)
                    field = needed_norm.get(norm)
                    if field is not None and field not in fixed_cols:
                        fixed_cols[field] = col_idx
            if "sku" in fixed_cols and date_cols:
                return row_idx, fixed_cols, date_cols
        return None, {}, {}
