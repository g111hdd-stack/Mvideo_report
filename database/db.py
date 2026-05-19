import time

from functools import wraps
from datetime import datetime, timedelta

from log_api.log import logger

from pyodbc import Error as PyodbcError

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from sqlalchemy import create_engine, func as f

from database.models import *
from database.data_classes import (
    DataMvideoCardProduct,
    DataMvideoAdvert,
    DataMvideoAdvertStatistic,
    DataMvideoStock,
    DataMvideoDistribution,
    DataMvideoAcquiring,
    DataMvideoMainTable,
)


def retry_on_exception(retries: int = 3, delay: int = 5):

    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            attempt = 0
            while attempt < retries:
                try:
                    result = func(self, *args, **kwargs)
                    return result
                except (OperationalError, PyodbcError) as e:
                    attempt += 1
                    print(f"Повторная попытка {attempt}/{retries}")
                    time.sleep(delay)
                    if hasattr(self, 'session'):
                        self.session.rollback()
                except Exception as e:
                    print(f"База данных. Произошла непредвиденая ошибка: {str(e)}.")
                    if hasattr(self, 'session'):
                        self.session.rollback()
                    raise e
            raise RuntimeError("База данных. Попытки подключения исчерпаны")

        return wrapper

    return decorator


class DbConnection:
    def __init__(self, url: str, echo: bool = False) -> None:
        self.engine = create_engine(
            url=url,
            echo=echo,
            pool_size=10,
            max_overflow=5,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,
            connect_args={
                "keepalives": 1,
                "keepalives_idle": 180,
                "keepalives_interval": 60,
                "keepalives_count": 20,
                "connect_timeout": 10,
            },
        )
        self.session = Session(self.engine)

    # === Создание таблиц ===

    @retry_on_exception()
    def create_mv_card_product(self) -> None:
        MvideoCardProduct.__table__.create(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_card_product успешно создана")

    @retry_on_exception()
    def create_mv_adverts_table(self) -> None:
        MvideoAdvertTable.__table__.create(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_adverts_table успешно создана")

    @retry_on_exception()
    def create_mv_statistic_advert(self) -> None:
        MvideoAdvertStatistic.__table__.create(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_statistic_advert успешно создана")

    @retry_on_exception()
    def create_mv_stocks(self) -> None:
        MvideoStock.__table__.create(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_stocks успешно создана")

    @retry_on_exception()
    def create_mv_log(self) -> None:
        MvideoDistribution.__table__.create(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_log успешно создана")

    @retry_on_exception()
    def create_mv_acquiring(self) -> None:
        MvideoAcquiring.__table__.create(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_acquiring успешно создана")

    @retry_on_exception()
    def create_mv_main_table(self) -> None:
        MvideoMainTable.__table__.create(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_main_table успешно создана")


    # === Удаление таблиц ===

    @retry_on_exception()
    def drop_mv_card_product(self) -> None:
        MvideoCardProduct.__table__.drop(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_card_product успешно удалена")

    @retry_on_exception()
    def drop_mv_adverts_table(self) -> None:
        MvideoAdvertTable.__table__.drop(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_adverts_table успешно удалена")

    @retry_on_exception()
    def drop_mv_advert_statistic(self) -> None:
        MvideoAdvertStatistic.__table__.drop(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_advert_statistic успешно удалена")

    @retry_on_exception()
    def drop_mv_stocks(self) -> None:
        MvideoStock.__table__.drop(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_stocks успешно удалена")

    @retry_on_exception()
    def drop_mv_distribution(self) -> None:
        MvideoDistribution.__table__.drop(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_distribution успешно удалена")

    @retry_on_exception()
    def drop_mv_acquiring(self) -> None:
        MvideoAcquiring.__table__.drop(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_acquiring успешно удалена")

    @retry_on_exception()
    def drop_mv_main_table(self) -> None:
        MvideoMainTable.__table__.drop(
            bind=self.engine,
            checkfirst=True,
        )

        logger.info("Таблица mv_main_table успешно удалена")


    @retry_on_exception()
    def get_card_products_meta(
            self,
            client_id: str,
            skus: list[str],
    ) -> dict[str, tuple[str | None, float | None]]:
        """
        Возвращает {sku: (vendor_code, commission_rate)} для SKU из списка
        в рамках client_id. SKU, которых нет в mv_card_product, в результате нет.

        commission_rate — это доля (0.13 для 13%), а не сумма.
        """
        if not skus:
            return {}

        rows = (
            self.session
            .query(
                MvideoCardProduct.sku,
                MvideoCardProduct.vendor_code,
                MvideoCardProduct.commission,
            )
            .filter(MvideoCardProduct.client_id == client_id)
            .filter(MvideoCardProduct.sku.in_(skus))
            .all()
        )
        return {
            row.sku: (
                row.vendor_code,
                float(row.commission) if row.commission is not None else None,
            )
            for row in rows
        }

    @retry_on_exception()
    def get_markets(self) -> list[Market]:
        markets = (
            self.session
            .query(Market)
            .filter(Market.marketplace == "МВидео")
            .all()
        )
        return markets

    @retry_on_exception()
    def get_phone_message(self, user: str, phone: str, marketplace: str) -> str:
        check = None
        for _ in range(20):
            check = self.session.query(PhoneMessage).filter(
                f.lower(PhoneMessage.user) == user.lower(),
                PhoneMessage.phone == phone,
                PhoneMessage.marketplace == marketplace
            ).order_by(PhoneMessage.time_request.desc()).first()

            if check is None:
                raise Exception('Ошибка получения сообщения')

            if check.message is not None:
                return check.message

            self.session.expire(check)
            time.sleep(5)

        self.session.delete(check)
        self.session.commit()
        raise Exception("Превышен лимит ожидания сообщения")

    @retry_on_exception()
    def check_phone_message(self, user: str, phone: str, time_request: datetime) -> None:
        for _ in range(20):
            check = self.session.query(PhoneMessage).filter(
                PhoneMessage.phone == phone,
                PhoneMessage.time_request >= time_request - timedelta(minutes=2),
                PhoneMessage.time_response.is_(None)
            ).all()

            if any([row.user.lower() == user.lower() for row in check]):
                raise Exception("Предыдущая аторизация не завершена.")

            if not check:
                break

            self.session.expire(check)
            time.sleep(5)
        else:
            raise Exception("Превышен лимит ожидания очереди на авторизацию")

    @retry_on_exception()
    def add_phone_message(self, user: str, phone: str, marketplace: str, time_request: datetime) -> None:
        user = self.session.query(User).filter(f.lower(User.user) == user.lower()).first()
        if user is None:
            raise Exception("Такого пользователя не существует")

        new = PhoneMessage(user=user.user,
                           phone=phone,
                           marketplace=marketplace,
                           time_request=time_request)
        self.session.add(new)
        self.session.commit()

    @retry_on_exception()
    def add_mvideo_card_products(
            self,
            products: list[DataMvideoCardProduct],
    ) -> None:
        if not products:
            logger.info("Нет товаров для записи в базу")
            return

        for product in products:
            new = MvideoCardProduct(
                sku=product.sku,
                client_id=product.client_id,
                vendor_code=product.vendor_code,
                link=product.link,
                brand=product.brand,
                product_group=product.product_group,
                price=product.price,
                discount_price=product.discount_price,
                commission=product.commission,
            )
            self.session.merge(new)

        self.session.commit()

        logger.info(f"Успешно добавлено/обновлено товаров в базе: {len(products)}")

    @retry_on_exception()
    def add_mvideo_adverts(
            self,
            adverts: list[DataMvideoAdvert],
    ) -> None:
        if not adverts:
            logger.info("Нет рекламных кампаний для записи в базу")
            return

        for advert in adverts:
            new = MvideoAdvertTable(
                id_advert=advert.id_advert,
                client_id=advert.client_id,
                name=advert.name,
                campaign_type=advert.campaign_type,
                payment_model=advert.payment_model,
                from_date=advert.from_date,
                status=advert.status,
                created_at=advert.created_at,
                updated_at=advert.updated_at,
            )
            self.session.merge(new)

        self.session.commit()

        logger.info(f"Успешно добавлено/обновлено рекламных кампаний: {len(adverts)}")

    @retry_on_exception()
    def add_mvideo_advert_statistics(
            self,
            statistics: list[DataMvideoAdvertStatistic],
    ) -> None:
        if not statistics:
            logger.info("Нет статистики рекламных кампаний для записи в базу")
            return

        for stat in statistics:
            stmt = insert(MvideoAdvertStatistic).values(
                advert_id=stat.advert_id,
                client_id=stat.client_id,
                sku=stat.sku,
                date=stat.date,
                views=stat.views,
                clicks=stat.clicks,
                baskets=stat.baskets,
                orders_count=stat.orders_count,
                sum_cost=stat.sum_cost,
            ).on_conflict_do_update(
                index_elements=["advert_id", "client_id", "sku", "date"],
                set_={
                    "views": stat.views,
                    "clicks": stat.clicks,
                    "baskets": stat.baskets,
                    "orders_count": stat.orders_count,
                    "sum_cost": stat.sum_cost,
                },
            )
            self.session.execute(stmt)

        self.session.commit()

        logger.info(
            f"Успешно добавлено/обновлено строк статистики рекламы: {len(statistics)}"
        )

    @retry_on_exception()
    def add_mvideo_stocks(
            self,
            stocks: list[DataMvideoStock],
    ) -> None:
        if not stocks:
            logger.info("Нет остатков МВидео для записи в базу")
            return

        for stock in stocks:
            stmt = insert(MvideoStock).values(
                date=stock.date,
                client_id=stock.client_id,
                sku=stock.sku,
                warehouse=stock.warehouse,
                city=stock.city,
                quantity_warehouse=stock.quantity_warehouse,
                cost=stock.cost,
            ).on_conflict_do_update(
                index_elements=["date", "client_id", "sku", "warehouse"],
                set_={
                    "city": stock.city,
                    "quantity_warehouse": stock.quantity_warehouse,
                    "cost": stock.cost,
                },
            )
            self.session.execute(stmt)

        self.session.commit()

        logger.info(
            f"Успешно добавлено/обновлено остатков МВидео: {len(stocks)}"
        )

    @retry_on_exception()
    def add_mvideo_distributions(
            self,
            rows: list[DataMvideoDistribution],
    ) -> None:
        if not rows:
            logger.info("Нет строк отчёта распределения для записи в базу")
            return

        for row in rows:
            stmt = insert(MvideoDistribution).values(
                date=row.date,
                client_id=row.client_id,
                sku=row.sku,
                tariff_rate=row.tariff_rate,
                quantity=row.quantity,
                cost=row.cost,
            ).on_conflict_do_update(
                index_elements=["date", "client_id", "sku"],
                set_={
                    "tariff_rate": row.tariff_rate,
                    "quantity": row.quantity,
                    "cost": row.cost,
                },
            )
            self.session.execute(stmt)

        self.session.commit()

        logger.info(
            f"Успешно добавлено/обновлено строк отчёта распределения: {len(rows)}"
        )

    @retry_on_exception()
    def add_mvideo_acquirings(
            self,
            rows: list[DataMvideoAcquiring],
    ) -> None:
        if not rows:
            logger.info("Нет строк acquiring-отчёта для записи в базу")
            return

        for row in rows:
            stmt = insert(MvideoAcquiring).values(
                date=row.date,
                client_id=row.client_id,
                sku=row.sku,
                quantity=row.quantity,
                sum=row.sum,
                total_sum=row.total_sum,
                transaction_type=row.transaction_type,
                cost=row.cost,
            ).on_conflict_do_update(
                index_elements=["date", "client_id", "sku", "transaction_type"],
                set_={
                    "quantity": row.quantity,
                    "sum": row.sum,
                    "total_sum": row.total_sum,
                    "cost": row.cost,
                },
            )
            self.session.execute(stmt)

        self.session.commit()

        logger.info(
            f"Успешно добавлено/обновлено строк acquiring-отчёта: {len(rows)}"
        )

    @retry_on_exception()
    def add_mvideo_main_tables(
            self,
            rows: list[DataMvideoMainTable],
    ) -> None:
        if not rows:
            logger.info("Нет строк mv_main_table для записи в базу")
            return

        for row in rows:
            stmt = insert(MvideoMainTable).values(
                accrual_date=row.accrual_date,
                client_id=row.client_id,
                type_of_transaction=row.type_of_transaction,
                sku=row.sku,
                delivery_schema=row.delivery_schema,
                vendor_code=row.vendor_code,
                sale=row.sale,
                quantities=row.quantities,
                commission=row.commission,
            ).on_conflict_do_update(
                index_elements=[
                    "accrual_date",
                    "client_id",
                    "type_of_transaction",
                    "delivery_schema",
                    "sku",
                ],
                set_={
                    "vendor_code": row.vendor_code,
                    "sale": row.sale,
                    "quantities": row.quantities,
                    "commission": row.commission,
                },
            )
            self.session.execute(stmt)

        self.session.commit()

        logger.info(
            f"Успешно добавлено/обновлено строк mv_main_table: {len(rows)}"
        )

