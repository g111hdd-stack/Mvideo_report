"""
Модули отчётов МВидео.

Каждый отчёт — отдельный класс в своём файле:
    - DistributionReport — отчёт о распределении (billing)
    - AcquiringReport    — отчёт по эквайрингу (billing)
    - StorageReport      — отчёт об услуге хранения (billing)
    - ConsolidatedReport — консолидированный отчёт (analytics)

Общие хелперы и BaseReport — в _common.py.
"""

from .distribution_report import DistributionReport
from .acquiring_report import AcquiringReport
from .storage_report import StorageReport
from .consolidated_report import ConsolidatedReport

__all__ = [
    "DistributionReport",
    "AcquiringReport",
    "StorageReport",
    "ConsolidatedReport",
]
