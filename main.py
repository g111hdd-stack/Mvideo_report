from sqlalchemy import text

from config import DB_ADMIN_URL, DB_ARRIS_URL
from database.db import DbConnection
from log_api.log import logger
from web_driver.wd import BrowserController
from web_driver.mvideo_reports import MvideoReports
from web_driver.mvideo_api import (
    MvideoApi,
    parse_mvideo_catalog_products,
    parse_mvideo_adverts,
    parse_mvideo_advert_statistics,
)

KEEP_BROWSER_OPEN = False


def main():
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

        for market in markets[0:1]:
            # if market.name_company != "Мкртчян Х.М.":
            #     continue
            driver = None

            try:
                logger.info(f"Начинаю авторизацию: {market.name_company}")

                driver = BrowserController(
                    market=market,
                    user="MvideoReport",
                    db_conn_admin=db_admin,
                    db_conn_arris=db_arris,
                )

                driver.load_url(url=market.marketplace_info.link)

                if not driver.is_browser_active():
                    logger.error(f"Браузер не активен: {market.name_company}")
                    continue

                logger.info(f"Авторизация завершена: {market.name_company}")

                # === Сбор данных через API ===
                api = MvideoApi(driver)

                # Получаем каталог товаров (со всех страниц)
                data = api.get_all_catalog_products()

                if data is not None:
                    logger.info(f"{market.name_company}: всего товаров: {data.get('totalElements')}")

                    products = parse_mvideo_catalog_products(
                        data=data,
                        client_id=market.client_id,
                    )

                    logger.info(f"{market.name_company}: товаров с ценой: {len(products)}")

                    db_arris.add_mvideo_card_products(products)

                    logger.info(f"{market.name_company}: товары обработаны")

                else:
                    logger.error(f"{market.name_company}: каталог не получен")

                # Получаем рекламные кампании
                campaigns_data = api.get_mvideo_campaigns()

                if campaigns_data is not None:
                    adverts = parse_mvideo_adverts(
                        data=campaigns_data,
                        client_id=market.client_id,
                    )

                    logger.info(f"{market.name_company}: распарсено кампаний: {len(adverts)}")

                    db_arris.add_mvideo_adverts(adverts)

                    logger.info(f"{market.name_company}: рекламные кампании сохранены в БД")

                    all_statistics = []

                    for advert in adverts:
                        stats_data = api.get_mvideo_campaign_stats_yesterday(
                            campaign_id=advert.id_advert,
                        )

                        if stats_data is None:
                            logger.error(
                                f"{market.name_company}: статистика кампании "
                                f"{advert.id_advert} не получена"
                            )
                            continue

                        campaign_statistics = parse_mvideo_advert_statistics(
                            data=stats_data,
                            advert_id=advert.id_advert,
                            client_id=market.client_id,
                        )

                        logger.info(
                            f"{market.name_company}: кампания {advert.id_advert}, "
                            f"строк статистики: {len(campaign_statistics)}"
                        )

                        all_statistics.extend(campaign_statistics)

                    db_arris.add_mvideo_advert_statistics(all_statistics)

                    logger.info(
                        f"{market.name_company}: всего строк статистики сохранено: "
                        f"{len(all_statistics)}"
                    )

                # === Скачивание billing-отчётов за текущий месяц ===
                reports = MvideoReports(driver, db_arris=db_arris)
                # reports.download_billing_reports_accumulating()

                # === Скачивание консолидированного отчёта (analytics) ===
                reports.download_consolidated_report()

                if KEEP_BROWSER_OPEN:
                    input("Браузер оставлен открытым. Нажмите Enter, чтобы закрыть...")

            except Exception as e:
                logger.error(f"Ошибка по компании {market.name_company}: {e}")

            finally:
                if driver is not None and not KEEP_BROWSER_OPEN:
                    driver.quit()

    except Exception as e:
        logger.error(f"Критическая ошибка main: {e}")

    finally:
        for db_conn in (db_admin, db_arris):
            if db_conn is not None:
                db_conn.session.close()


if __name__ == "__main__":
    main()
