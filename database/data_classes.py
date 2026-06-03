from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class DataMvideoCardProduct:
    sku: str
    client_id: str
    vendor_code: Optional[str]
    link: Optional[str]
    brand: Optional[str]
    product_group: Optional[str]
    discount_price: Optional[float]
    price: Optional[float]
    commission: Optional[float]


@dataclass
class DataMvideoAdvert:
    id_advert: str
    client_id: str
    name: Optional[str]
    campaign_type: Optional[str]
    payment_model: Optional[str]
    status: Optional[str]
    from_date: date
    created_at: date
    updated_at: date


@dataclass
class DataMvideoAdvertStatistic:
    advert_id: str
    client_id: str
    sku: str
    date: date
    views: Optional[int]
    clicks: Optional[int]
    baskets: Optional[int]
    orders_count: Optional[int]
    sum_cost: Optional[float]


@dataclass
class DataMvideoStock:
    date: date
    client_id: str
    sku: str
    warehouse: Optional[str]
    city: Optional[str]
    quantity_warehouse: Optional[int]
    cost: Optional[float]


@dataclass
class DataMvideoDistribution:
    date: date
    client_id: str
    sku: str
    quantity: Optional[int]
    cost: Optional[float]


@dataclass
class DataMvideoAcquiring:
    date: date
    client_id: str
    sku: str
    receipt_number: Optional[str]
    quantity: Optional[int]
    sum: Optional[float]
    total_sum: Optional[float]
    transaction_type: Optional[str]
    cost: Optional[float]


@dataclass
class DataMvideoMainTable:
    accrual_date: date
    client_id: str
    type_of_transaction: str         # 'delivered' / 'cancelled'
    sku: str
    delivery_schema: Optional[str]
    vendor_code: Optional[str]       # подтягивается из mv_card_product
    sale: Optional[float]            # сумма продаж
    quantities: Optional[int]        # количество продаж
    commission: Optional[float]      # sale * commission из mv_card_product
