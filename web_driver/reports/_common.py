"""
Общие хелперы и базовый класс для отчётов МВидео.

Содержит:
- BaseReport — базовый класс с логированием, чтением xlsx, поиском заголовков.
- Свободные функции-хелперы для парсинга ячеек (to_int, to_float, to_date и т.д.).
- Утилиты для специфических полей (split_macroregion, try_extract_date).
"""

import re
from datetime import datetime, date as date_type

import pandas as pd

from log_api.log import logger


# Имя листа в xlsx, на котором лежат данные billing-отчётов
DETAIL_SHEET_NAME = "Детализированный отчет"


# ===========================================================================
# Чистые функции-хелперы (без логирования)
# ===========================================================================

def normalize_header(value: str) -> str:
    """Приводит заголовок к каноничному виду для сравнения."""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def cell_from_row(row, idx):
    """Безопасное извлечение ячейки pandas Series по индексу."""
    if idx is None or idx >= len(row):
        return None
    val = row.iloc[idx]
    if pd.isna(val):
        return None
    return val


def to_str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def to_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_date(v) -> date_type | None:
    """Извлекает date из ячейки. Принимает datetime, date, Timestamp, строки."""
    if v is None:
        return None
    if pd.isna(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date_type):
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


def split_macroregion(value) -> tuple[str | None, str | None]:
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


def try_extract_date(value) -> date_type | None:
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
    if isinstance(value, date_type):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            return datetime.strptime(s, "%d.%m.%Y").date()
        except ValueError:
            pass
    return None


# ===========================================================================
# BaseReport — общая логика для всех отчётов
# ===========================================================================

class BaseReport:
    """
    Базовый класс отчёта. Хранит market + db_arris, логирует с префиксом компании,
    умеет открывать лист с данными (по умолчанию 'Детализированный отчет') и
    искать строку заголовков. Подкласс может переопределить SHEET_NAME под свой xlsx.
    """

    # Имя листа в xlsx, который читает _read_detail_sheet.
    # Подкласс может переопределить (например, 'Отчёт по объектам' для консолидированного).
    SHEET_NAME: str = DETAIL_SHEET_NAME

    # Короткое имя отчёта для логов (подкласс задаёт своё)
    LABEL: str = ""

    # Подстроки для поиска xlsx-файла в локальной папке (case-insensitive)
    PATTERNS: tuple[str, ...] = ()

    def __init__(self, market=None, db_arris=None) -> None:
        self.market = market
        self.db_arris = db_arris

    # --- логирование ---
    def _info(self, msg: str) -> None:
        prefix = f"{self.market.name_company}: " if self.market is not None else ""
        logger.info(f"{prefix}{msg}")

    def _error(self, msg: str) -> None:
        prefix = f"{self.market.name_company}: " if self.market is not None else ""
        logger.error(f"{prefix}{msg}")

    # --- работа с xlsx ---
    def _read_detail_sheet(self, file_path: str, label: str):
        """
        Открывает xlsx через pandas+calamine и возвращает DataFrame листа
        self.SHEET_NAME без заголовка (header=None). None при ошибке.
        """
        try:
            xls = pd.ExcelFile(file_path, engine="calamine")
        except Exception as e:
            self._error(f"{label}: не удалось открыть xlsx: {e}")
            return None

        target = normalize_header(self.SHEET_NAME)
        sheet_match = next(
            (s for s in xls.sheet_names if normalize_header(s) == target),
            None,
        )
        if sheet_match is None:
            self._error(
                f"{label}: лист '{self.SHEET_NAME}' не найден "
                f"(есть листы: {xls.sheet_names})"
            )
            return None

        try:
            return xls.parse(sheet_match, header=None)
        except Exception as e:
            self._error(f"{label}: не удалось прочитать лист '{sheet_match}': {e}")
            return None

    def _find_headers_in_df(
            self,
            df,
            col_names: dict[str, str],
            required_fields: tuple[str, ...] = ("sku", "date"),
    ) -> tuple[int | None, dict[str, int]]:
        """
        Сканирует первые 30 строк DataFrame, ищет ту, где находятся точные
        названия колонок (см. col_names). Возвращает (индекс строки, col_map).
        Заголовки считаются найденными только если присутствуют все required_fields.
        """
        expected = {
            field: normalize_header(name)
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
                norm = normalize_header(cell)
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

    def _log_missing_columns(
            self,
            label: str,
            col_names: dict[str, str],
            found: dict[str, int],
    ) -> None:
        """Пишет error для каждой ожидаемой колонки, которой не нашлось."""
        missing = set(col_names.keys()) - set(found.keys())
        for field in missing:
            self._error(
                f"{label}: колонка '{col_names[field]}' не найдена "
                f"(возможно, название изменилось; ожидалось поле '{field}')"
            )
