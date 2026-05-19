from sqlalchemy import Column, DateTime, Date, String, Integer, BigInteger, Numeric, func, Text, Float
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import UniqueConstraint, MetaData, Identity, ForeignKey


metadata = MetaData()
Base = declarative_base(metadata=metadata)

class Client(Base):
    """Модель таблицы clients."""
    __tablename__ = 'clients'

    client_id = Column(String(length=255), primary_key=True)
    api_key = Column(String(length=1000), nullable=False)
    marketplace = Column(String(length=255), nullable=False)
    name_company = Column(String(length=255), nullable=False)
    entrepreneur = Column(String(length=255), nullable=False)
    tax_type = Column(String(length=255), nullable=False)


class Market(Base):
    __tablename__ = 'markets'

    id = Column(Integer, Identity(), primary_key=True)
    marketplace = Column(String(length=255),
                         ForeignKey('marketplaces.marketplace', ondelete='CASCADE', onupdate='CASCADE'), nullable=False)
    name_company = Column(String(length=255), nullable=False)
    phone = Column(String(length=255), ForeignKey('connects.phone', ondelete='CASCADE', onupdate='CASCADE'),
                   nullable=False)
    entrepreneur = Column(String(length=255), nullable=True)
    client_id = Column(String(length=255), nullable=True)

    marketplace_info = relationship("Marketplace", back_populates="markets")
    connect_info = relationship("Connect", back_populates="markets")

    __table_args__ = (
        UniqueConstraint('marketplace', 'name_company', 'phone', name='markets_unique'),
        UniqueConstraint('marketplace', 'name_company', name='market_unique'),
    )


class Marketplace(Base):
    __tablename__ = 'marketplaces'

    marketplace = Column(String(length=255), primary_key=True, nullable=False)
    link = Column(String(length=1000), nullable=False)
    domain = Column(String(length=255), nullable=False)

    markets = relationship("Market", back_populates="marketplace_info")


class Connect(Base):
    __tablename__ = 'connects'

    phone = Column(String(length=255), primary_key=True, nullable=False)
    proxy = Column(String(length=255), nullable=False)
    mail = Column(String(length=255), nullable=False)
    token = Column(String(length=255), nullable=False)
    pass_mail = Column(String(length=255), nullable=True)

    markets = relationship("Market", back_populates="connect_info")

    __table_args__ = (
        UniqueConstraint('phone', 'proxy', name='connects_unique'),
    )


class User(Base):
    __tablename__ = 'users'

    user = Column(String(length=255), primary_key=True, nullable=False)
    password = Column(String(length=255), nullable=False)
    name = Column(String(length=255), default=None, nullable=True)
    group = Column(String(length=255), ForeignKey('group_table.group', ondelete='CASCADE', onupdate='CASCADE'),
                   nullable=False)

class PhoneMessage(Base):
    __tablename__ = 'phone_message'

    id = Column(Integer, Identity(), primary_key=True)
    user = Column(String(length=255), ForeignKey('users.user', ondelete='SET NULL', onupdate='CASCADE'),
                  nullable=False)
    phone = Column(String(length=255), ForeignKey('connects.phone', ondelete='CASCADE', onupdate='CASCADE'),
                   nullable=False)
    marketplace = Column(String(length=255),
                         ForeignKey('marketplaces.marketplace', ondelete='CASCADE', onupdate='CASCADE'), nullable=False)
    time_request = Column(DateTime, nullable=False)
    time_response = Column(DateTime, default=None, nullable=True)
    message = Column(String(length=255), default=None, nullable=True)

class MvideoCardProduct(Base):
    __tablename__ = 'mv_card_product'

    sku = Column(Text, primary_key=True, nullable=False)
    client_id = Column(
        String(length=255),
        ForeignKey(
            'clients.client_id',
            name='mv_card_product_clients_fk',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        primary_key=True,
        nullable=False,
    )

    vendor_code = Column(String(length=255), nullable=True)
    link = Column(String, nullable=True)
    brand = Column(String(length=255), nullable=True)
    product_group = Column(String(length=255), nullable=True)

    discount_price = Column(Numeric(12, 2), nullable=True)
    price = Column(Numeric(12, 2), nullable=True)
    commission = Column(Numeric(12, 4), nullable=True)

    created_at = Column(Date, server_default=func.current_date(), nullable=True)
    updated_at = Column(Date, server_default=func.current_date(), onupdate=func.current_date(), nullable=True)


class MvideoAdvertTable(Base):
    __tablename__ = 'mv_adverts_table'

    id_advert = Column(Text, primary_key=True, nullable=False)
    client_id = Column(
        String(length=255),
        ForeignKey(
            'clients.client_id',
            name='mv_adverts_table_clients_fk',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        nullable=False,
    )

    name = Column(String(length=255), nullable=True)
    campaign_type = Column(String(length=255), nullable=True)
    payment_model = Column(String(length=255), nullable=True)
    status = Column(String(length=255), nullable=True)

    from_date = Column(Date, nullable=False)
    created_at = Column(Date, nullable=False)
    updated_at = Column(Date, nullable=False)


class MvideoAdvertStatistic(Base):
    __tablename__ = 'mv_statistic_advert'

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    advert_id = Column(
        Text,
        ForeignKey(
            'mv_adverts_table.id_advert',
            name='mv_statistic_advert_adverts_table_fk',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        nullable=False,
    )
    client_id = Column(
        String(length=255),
        ForeignKey(
            'clients.client_id',
            name='mv_statistic_advert_clients_fk',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        nullable=False,
    )

    sku = Column(Text, nullable=False)
    date = Column(Date, nullable=False)

    views = Column(BigInteger, nullable=True)
    clicks = Column(BigInteger, nullable=True)
    baskets = Column(BigInteger, nullable=True)
    orders_count = Column(BigInteger, nullable=True)
    sum_cost = Column(Numeric(12, 2), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            'advert_id',
            'client_id',
            'sku',
            'date',
            name='mv_advert_statistic_unique'
        ),
    )

class MvideoStock(Base):
    __tablename__ = 'mv_stocks'

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    date = Column(Date, nullable=False)
    client_id = Column(
        String(length=255),
        ForeignKey(
            'clients.client_id',
            name='mv_stocks_clients_fk',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        nullable=False,
    )

    sku = Column(Text, nullable=False)
    warehouse = Column(String(length=255), nullable=True)
    city = Column(String(length=255), nullable=True)
    quantity_warehouse = Column(BigInteger, nullable=True)
    cost = Column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            'date',
            'client_id',
            'sku',
            'warehouse',
            name='mv_stocks_unique',
        ),
    )

class MvideoDistribution(Base):
    __tablename__ = 'mv_log'

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    date = Column(Date, nullable=False)
    client_id = Column(
        String(length=255),
        ForeignKey(
            'clients.client_id',
            name='mv_log_clients_fk',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        nullable=False,
    )
    sku = Column(String(length=255), nullable=False)

    tariff_rate = Column(Float, nullable=True)
    quantity = Column(BigInteger, nullable=True)
    cost = Column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            'date',
            'client_id',
            'sku',
            name='mv_distribution_unique',
        ),
    )


class MvideoAcquiring(Base):
    __tablename__ = 'mv_acquiring'

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    date = Column(Date, nullable=False)
    client_id = Column(
        String(length=255),
        ForeignKey(
            'clients.client_id',
            name='mv_acquiring_clients_fk',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        nullable=False,
    )
    sku = Column(String(length=255), nullable=False)

    quantity = Column(BigInteger, nullable=True)
    sum = Column(Float, nullable=True)
    total_sum = Column(Float, nullable=True)
    transaction_type = Column(String(length=255), nullable=True)
    cost = Column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            'date',
            'client_id',
            'sku',
            'transaction_type',
            name='mv_acquiring_unique',
        ),
    )


class MvideoMainTable(Base):
    """
    Сводная таблица продаж: одна строка на (accrual_date, client_id, sku, type_of_transaction).
    Поля заполняются из консолидированного отчёта; vendor_code и commission подтягиваются
    по SKU из mv_card_product.
    """
    __tablename__ = 'mv_main_table'

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    accrual_date = Column(Date, nullable=False)             # дата продажи
    client_id = Column(
        String(length=255),
        ForeignKey(
            'clients.client_id',
            name='mv_main_table_clients_fk',
            ondelete='CASCADE',
            onupdate='CASCADE',
        ),
        nullable=False,
    )                                                        # айди кабинета
    type_of_transaction = Column(String(length=255), nullable=False)
    # 'delivered' если сумма положительная, 'cancelled' если отрицательная

    vendor_code = Column(String(length=255), nullable=True)  # из mv_card_product
    delivery_schema = Column(String(length=255), nullable=True)  # «Тип объекта в документе»

    sku = Column(String(length=255), nullable=False)

    sale = Column(Numeric(14, 2), nullable=True)             # сумма продаж
    quantities = Column(BigInteger, nullable=True)           # количество продаж
    commission = Column(Numeric(14, 2), nullable=True)       # sale * commission из mv_card_product

    __table_args__ = (
        UniqueConstraint(
            'accrual_date',
            'client_id',
            'type_of_transaction',
            'delivery_schema',
            'sku',
            name='mv_main_table_unique',
        ),
    )









