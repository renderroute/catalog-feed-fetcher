from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .woo_shop_html import fetch_woocommerce_shop_html

# Keep payload lean — omit bulky HTML fields from Store API responses.
FIELDS = (
    "id,name,permalink,sku,prices,images,categories,brands,"
    "is_in_stock,on_sale,gtin,global_unique_id"
)
# After homepage cookies: try the fast size first, then step down if Cloudflare still 403s.
COOKIE_PAGE_SIZES = (50, 25, 10)


class WooNeedsCookieRetry(RuntimeError):
    """Fast path hit HTTP 403 — caller should queue the shop for homepage-cookie fetch."""

    def __init__(self) -> None:
        super().__init__("Woo Store API HTTP 403 (likely bot/CDN block)")


def _endpoint(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if "/wp-json/wc/store" in base:
        return base.split("?")[0]
    return urljoin(base + "/", "wp-json/wc/store/v1/products")


def _map_product(product: dict[str, Any]) -> dict[str, Any] | None:
    product_id = str(product.get("id") or "").strip()
    if not product_id:
        return None
    prices = product.get("prices") if isinstance(product.get("prices"), dict) else {}
    raw_price = prices.get("regular_price") or prices.get("price")
    raw_sale = prices.get("sale_price") if product.get("on_sale") else None
    currency_minor = int(prices.get("currency_minor_unit") or 0)

    def from_minor(v: Any) -> float | None:
        m = money(v)
        if m is None:
            return None
        if currency_minor > 0:
            return m / (10 ** currency_minor)
        return m

    price = from_minor(raw_price)
    sale_price = from_minor(raw_sale) if raw_sale not in (None, "", "0") else None
    if sale_price is not None and price is not None and sale_price >= price:
        sale_price = None
    image_url = ""
    images = product.get("images") or []
    if images and isinstance(images[0], dict):
        image_url = str(images[0].get("src") or "")
    brand = ""
    brands = product.get("brands") or []
    if brands and isinstance(brands[0], dict):
        brand = str(brands[0].get("name") or "")
    in_stock = bool(product.get("is_in_stock"))
    gtin = str(product.get("gtin") or product.get("global_unique_id") or "").strip()
    return {
        "merchant_item_id": product_id,
        "title": str(product.get("name") or "").strip(),
        "product_url": str(product.get("permalink") or "").strip(),
        "image_url": image_url,
        "product_type": "",
        "brand": brand,
        "mpn": str(product.get("sku") or "").strip(),
        "gtin": gtin,
        "availability": "in_stock" if in_stock else "out_of_stock",
        "price": price,
        "sale_price": sale_price,
        "effective_price": sale_price if sale_price is not None else price,
        "feed_condition": "new",
    }


def _shop_origin(base_url: str) -> str:
    raw = (base_url or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme or 'https'}://{parsed.netloc}/"


def _browser_session():
    """Same helper the HTML price scraper uses (not a full Chrome window)."""
    import cloudscraper

    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )


def _warmup_homepage(session: requests.Session, origin: str) -> None:
    if not origin:
        raise RuntimeError("Missing shop origin for homepage warmup")
    print(f"  Opening shop homepage for CDN cookies: {origin}", flush=True)
    session.get(origin, timeout=90)
    time.sleep(2.0)


def _fetch_pages(
    session: requests.Session,
    *,
    endpoint: str,
    per_page: int | None,
    delay_seconds: float,
    use_bot_user_agent: bool = True,
    extra_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    page = 1
    items: list[dict[str, Any]] = []
    expected = int(per_page) if per_page else 10

    while True:
        params: dict[str, Any] = {"page": page, "_fields": FIELDS}
        if extra_params:
            params.update(extra_params)
        if per_page:
            params["per_page"] = int(per_page)
        headers = {"Accept": "application/json"}
        if use_bot_user_agent:
            headers["User-Agent"] = USER_AGENT
        resp = session.get(
            endpoint,
            params=params,
            headers=headers,
            timeout=90,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After") or "60")
            raise RuntimeError(f"Woo Store API HTTP 429 (retry_after={retry_after})")
        if resp.status_code == 403:
            raise RuntimeError("Woo Store API HTTP 403 (likely bot/CDN block)")
        resp.raise_for_status()
        products = resp.json()
        if not isinstance(products, list):
            raise RuntimeError("Unexpected Woo Store API shape")
        if page == 1 and len(products) == 0:
            break

        for product in products:
            if isinstance(product, dict):
                mapped = _map_product(product)
                if mapped:
                    items.append(mapped)

        total_pages = 0
        try:
            total_pages = int(resp.headers.get("X-WP-TotalPages") or "0")
        except ValueError:
            total_pages = 0
        if total_pages > 0:
            if page >= total_pages:
                break
        elif len(products) < expected:
            break
        page += 1
        time.sleep(max(0.0, float(delay_seconds)))

    return items


def fetch_woocommerce_store_api(
    session: requests.Session,
    *,
    base_url: str,
    per_page: int | None = 50,
    delay_seconds: float = 2.0,
    omit_per_page: bool = False,
    cookie_retry: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetch Woo Store API products.

    Fast path (cookie_retry=False): per_page 50. HTTP 403 raises WooNeedsCookieRetry
    (no hour-long 10-per-page crawl).

    Cookie path: open homepage, then try 50 → 25 → 10.
    """
    endpoint = _endpoint(base_url)
    origin = _shop_origin(base_url)
    page_size = 50 if per_page in (None, 0) else int(per_page)
    if omit_per_page and not cookie_retry:
        page_size = 50

    if not cookie_retry:
        try:
            return _fetch_pages(
                session,
                endpoint=endpoint,
                per_page=page_size,
                delay_seconds=delay_seconds,
                use_bot_user_agent=True,
            )
        except RuntimeError as exc:
            if "HTTP 403" not in str(exc):
                raise
            raise WooNeedsCookieRetry() from exc

    print("  Woo HTTP 403; homepage first then API (cookie retry)", flush=True)
    origin_ok = origin
    try:
        browser = _browser_session()
        _warmup_homepage(browser, origin_ok)
    except Exception as cookie_exc:
        print(f"  Homepage cookie warmup failed ({cookie_exc})", flush=True)
        raise

    last_exc: BaseException | None = None
    for size in COOKIE_PAGE_SIZES:
        print(f"  Cookie path trying per_page={size}", flush=True)
        try:
            return _fetch_pages(
                browser,
                endpoint=endpoint,
                per_page=size,
                delay_seconds=delay_seconds,
                use_bot_user_agent=False,
            )
        except RuntimeError as exc:
            last_exc = exc
            if "HTTP 403" not in str(exc):
                raise
            print(f"  per_page={size} still HTTP 403", flush=True)
    print("  Cookie path API still 403; trying rest_route", flush=True)
    for size in COOKIE_PAGE_SIZES:
        print(f"  rest_route trying per_page={size}", flush=True)
        try:
            return _fetch_pages(
                browser,
                endpoint=origin_ok,
                per_page=size,
                delay_seconds=delay_seconds,
                use_bot_user_agent=False,
                extra_params={"rest_route": "/wc/store/v1/products"},
            )
        except RuntimeError as exc:
            last_exc = exc
            if "HTTP 403" not in str(exc):
                raise
            print(f"  rest_route per_page={size} still HTTP 403", flush=True)
    try:
        return fetch_woocommerce_shop_html(
            browser,
            origin=origin_ok,
            delay_seconds=min(1.0, float(delay_seconds) if delay_seconds else 1.0),
        )
    except Exception as html_exc:
        print(f"  Shop HTML fallback failed ({html_exc})", flush=True)
        if last_exc is not None:
            raise last_exc
        raise
