import json
import requests

from playwright.sync_api import TimeoutError as PwTimeoutError
from datetime import datetime, timedelta

from database.data_classes import (DataMvideoCardProduct, DataMvideoAdvert, DataMvideoAdvertStatistic, DataMvideoStock,)
from log_api.log import logger


class MvideoApi:
    BASE = "https://sellers.mvideo.ru"

    def __init__(self, driver) -> None:
        self.driver = driver
        self.page = driver.page
        self.context = driver.context
        self.market = driver.market
        self._token: str | None = None

    # --- логирование ---
    def _info(self, msg: str) -> None:
        logger.info(f"{self.market.name_company}: {msg}")

    def _error(self, msg: str) -> None:
        logger.error(f"{self.market.name_company}: {msg}")

    # --- токен ---
    def _get_token(self, force: bool = False) -> str | None:
        if self._token and not force:
            return self._token
        try:
            kauth = self.page.evaluate('() => localStorage.getItem("kauth")')
            self._token = json.loads(kauth or "{}").get("accessToken")
            if not self._token:
                self._error("accessToken не найден в localStorage kauth")
            return self._token
        except Exception as e:
            self._error(f"ошибка получения accessToken: {e}")
            return None

    # --- сессия ---
    def _build_session(self, referer: str) -> requests.Session | None:
        token = self._get_token()
        if not token:
            return None
        s = requests.Session()
        for c in self.context.cookies():
            s.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain"),
                path=c.get("path", "/"),
            )
        s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "Origin": self.BASE,
            "Authorization": f"Bearer {token}",
        })
        return s

    # --- универсальный запрос ---
    def _api(self, method: str, url: str, referer: str, *, params: dict | None = None, json_body: dict | None = None,
             ) -> dict | list | None:
        session = self._build_session(referer)
        if session is None:
            return None

        try:
            r = session.request(method, url, params=params, json=json_body, timeout=60)
            self._info(f"{method} {r.url} status={r.status_code}")
            if r.status_code != 200:
                self._error(f"ошибка API {url} {r.status_code}: {r.text[:1000]}")
                return None
            return r.json()
        except requests.RequestException as e:
            self._error(f"ошибка запроса {url}: {e}")
        except ValueError:
            self._error(f"API {url} вернул не JSON")
        return None

    # --- публичные методы ---
    def open_catalog_and_get_products(self, page: int = 0, size: int = 50):
        catalog_url = f"{self.BASE}/mpa/products/catalog"
        try:
            self.page.goto(catalog_url, wait_until="domcontentloaded", timeout=90_000)
        except PwTimeoutError:
            self._info(f"каталог не дождался загрузки, продолжаю. URL: {self.page.url}")
        self.page.wait_for_timeout(5000)

        return self._api(
            "GET", f"{self.BASE}/api/catalog", referer=catalog_url,
            params={
                "page": page, "size": size,
                "sort": "createdDate,desc",
                "filter": "productType:MARKETPLACE,archived:false",
                "fields": "+prices",
            },
        )

    def get_mvideo_campaigns(self):
        return self._api(
            "GET", f"{self.BASE}/seller-api/v1/campaigns",
            referer=f"{self.BASE}/mpa/marketing/campaigns",
        )

    def get_mvideo_campaign_stats_yesterday(self, campaign_id: str):
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        return self._api(
            "GET", f"{self.BASE}/seller-api/v1/campaigns/{campaign_id}/stats",
            referer=f"{self.BASE}/mpa/marketing/campaigns",
            params={"from_date": yesterday, "to_date": yesterday},
        )

    def get_mvideo_stock_movements(self, page: int = 0, size: int = 1000):
        return self._api(
            "POST", f"{self.BASE}/api/productmovements/search",
            referer=f"{self.BASE}/mpa/products/productmovements",
            json_body={"pageable": {"page": page, "size": size, "sort": []}},
        )


# === Хелперы для парсеров ===

def _str(v):
    return None if v is None else str(v).strip()


def _int(v):
    return None if v is None else int(v)


def _float(v):
    return None if v is None else float(v)


def _dt(v):
    if v is None:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)


def _date(v):
    dt = _dt(v)
    return None if dt is None else dt.date()


# === Парсеры ===

def parse_mvideo_catalog_products(
        data: dict,
        client_id: str,
) -> list[DataMvideoCardProduct]:
    products: list[DataMvideoCardProduct] = []

    for item in data.get("content", []):
        material = item.get("materialNumber")
        current_price = (item.get("prices") or {}).get("currentPrice") or {}
        price = current_price.get("price")

        if material is None or price is None:
            continue

        commission = item.get("commission")
        if commission is not None:
            commission = float(commission) / 100

        products.append(DataMvideoCardProduct(
            sku=str(material),
            client_id=client_id,
            vendor_code=_str(item.get("manufacturerCode")),
            link=_str(item.get("linkMvideo")),
            brand=_str(item.get("brandName")),
            product_group=_str(item.get("groupName")),
            price=_float(price),
            discount_price=_float(current_price.get("promoPrice")),
            commission=_float(commission),
        ))

    return products


def parse_mvideo_adverts(
        data: list[dict],
        client_id: str,
) -> list[DataMvideoAdvert]:
    adverts: list[DataMvideoAdvert] = []

    for item in data:
        campaign_id = item.get("campaign_id")
        if campaign_id is None:
            continue

        adverts.append(DataMvideoAdvert(
            id_advert=str(campaign_id),
            client_id=client_id,
            name=_str(item.get("name")),
            campaign_type=_str(item.get("campaign_type")),
            payment_model=_str(item.get("payment_model")),
            from_date=_date(item.get("from_date")),
            status=_str(item.get("status")),
            created_at=_date(item.get("created_at")),
            updated_at=_date(item.get("updated_at")),
        ))

    return adverts


def parse_mvideo_advert_statistics(
        data: list[dict],
        advert_id: str,
        client_id: str,
) -> list[DataMvideoAdvertStatistic]:
    if not isinstance(data, list):
        return []

    statistics: list[DataMvideoAdvertStatistic] = []

    for item in data:
        sku = item.get("sku_id")
        if sku is None:
            continue

        for stat in item.get("stats") or []:
            stat_date = _date(stat.get("date"))
            if stat_date is None:
                continue

            views = _int(stat.get("shows"))
            clicks = _int(stat.get("clicks"))
            baskets = _int(stat.get("baskets"))
            orders_count = _int(stat.get("orders"))

            summa = _float(stat.get("summa"))
            sum_cost = summa / 100 if summa is not None else None

            # Пропускаем пустые строки: все метрики 0 или None
            if not any((views, clicks, baskets, orders_count, sum_cost)):
                continue

            statistics.append(DataMvideoAdvertStatistic(
                advert_id=str(advert_id),
                client_id=client_id,
                sku=str(sku),
                date=stat_date,
                views=views,
                clicks=clicks,
                baskets=baskets,
                orders_count=orders_count,
                sum_cost=sum_cost,
            ))

    return statistics


def parse_mvideo_stocks(
        data: dict | list,
        client_id: str,
        stock_date=None,
        articles_by_sku: dict[str, str] | None = None,
) -> list[DataMvideoStock]:
    if stock_date is None:
        stock_date = datetime.now().date()

    if isinstance(data, dict):
        items = data.get("content") or []
    elif isinstance(data, list):
        items = data
    else:
        return []

    stocks: list[DataMvideoStock] = []

    for item in items:
        sku = item.get("zmaterial")
        if sku is None:
            continue

        sku_str = str(sku)
        vendor_code = articles_by_sku.get(sku_str) if articles_by_sku else None

        stocks.append(DataMvideoStock(
            date=stock_date,
            client_id=client_id,
            sku=sku_str,
            vendor_code=vendor_code,
            warehouse=_str(item.get("zplant")),
            city=_str(item.get("city")),
            quantity_warehouse=_int(item.get("qty")),
            quantity_to_client=0,
            quantity_from_client=0,
        ))

    return stocks


