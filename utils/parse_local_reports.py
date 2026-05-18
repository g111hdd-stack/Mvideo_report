"""
Скрипт для парсинга уже скачанных billing-отчётов МВидео из локальной папки
и записи в БД. Используется, чтобы догрузить отчёты за прошлые периоды.

БЕЗ браузера и авторизации — работает только с локальными xlsx и БД.

Как использовать:
    1. В CONFIG ниже укажи папку с xlsx-файлами для каждого client_id
       и дату периода (period_date — первый день месяца отчёта).
    2. Файлы в папке должны называться: distribution.xlsx, acquiring.xlsx, storage.xlsx
       (отсутствующие просто пропускаются).
    3. Запусти:  python parse_local_reports.py
"""

from datetime import date

from sqlalchemy import text

from config import DB_ADMIN_URL, DB_ARRIS_URL
from database.db import DbConnection
from log_api.log import logger
from web_driver.mvideo_reports import MvideoReports


# ============================================================
# CONFIG — отредактируй под свою задачу
# ============================================================

# Дата периода (первый день месяца, за который отчёт)
PERIOD_DATE: date = date(2026, 4, 30)

# Папки с локальными xlsx-файлами по client_id
# Ключ — client_id (из таблицы markets), значение — путь к папке
LOCAL_DIRS: dict[str, str] = {
    "K000071171": r"C:\Users\Witcherald\PycharmProjects\Mvideo_report\report\Бурчян Г.С\2026-04-30",
    # "K000073787": r"C:\path\to\other\client\folder",
}

# ============================================================


def main() -> None:
    db_admin = None
    db_arris = None

    try:
        db_admin = DbConnection(url=DB_ADMIN_URL)
        db_arris = DbConnection(url=DB_ARRIS_URL)

        logger.info(
            f"Проверка подключения ADMIN: "
            f"{db_admin.session.execute(text('SELECT 1')).scalar()}"
        )
        logger.info(
            f"Проверка подключения ARRIS: "
            f"{db_arris.session.execute(text('SELECT 1')).scalar()}"
        )

        markets = db_admin.get_markets()
        markets_by_client = {m.client_id: m for m in markets}

        # фильтруем только те client_id, что прописаны в LOCAL_DIRS
        targets = [
            (cid, path) for cid, path in LOCAL_DIRS.items()
            if cid in markets_by_client
        ]

        missing = set(LOCAL_DIRS) - set(markets_by_client)
        if missing:
            logger.error(
                f"Следующие client_id из LOCAL_DIRS не найдены в markets: "
                f"{sorted(missing)} — будут пропущены"
            )

        if not targets:
            logger.error("Нет ни одного client_id для обработки. Выход.")
            return

        for client_id, directory in targets:
            market = markets_by_client[client_id]

            try:
                logger.info(
                    f"Начинаю локальный парсинг для {market.name_company} "
                    f"(client_id={client_id})"
                )

                reports = MvideoReports(
                    driver=None,
                    db_arris=db_arris,
                    market=market,
                )

                result = reports.parse_local_directory(
                    directory=directory,
                    period_date=PERIOD_DATE,
                )

                logger.info(
                    f"{market.name_company}: итого записано строк — "
                    f"distribution={result.get('distribution', 0)}, "
                    f"acquiring={result.get('acquiring', 0)}, "
                    f"storage={result.get('storage', 0)}"
                )

            except Exception as e:
                logger.error(f"Ошибка по компании {market.name_company}: {e}")

    except Exception as e:
        logger.error(f"Критическая ошибка parse_local_reports: {e}")

    finally:
        for db_conn in (db_admin, db_arris):
            if db_conn is not None:
                db_conn.session.close()


if __name__ == "__main__":
    main()
