from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin

import requests

from . import USER_AGENT, money

# Keep payload lean — omit bulky HTML fields from Store API responses.
FIELDS = (
    "id,name,permalink,sku,prices,images,categories,brands,"
    "is_in_stock,on_sale,gtin,global_unique_id"
)


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


def _fetch_pages(
    session: requests.Session,
    *,
    endpoint: str,
    per_page: int | None,
    delay_seconds: float,
) -> list[dict[str, Any]]:
    page = 1
    items: list[dict[str, Any]] = []
    expected = int(per_page) if per_page else 10

    while True:
        params: dict[str, Any] = {"page": page, "_fields": FIELDS}
        if per_page:
            params["per_page"] = int(per_page)
        resp = session.get(
            endpoint,
            params=params,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
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
) -> list[dict[str, Any]]:
    """
    Fetch Woo Store API products.
    Default uses per_page (fast). On HTTP 403, retries without per_page (Cloudflare-safer).
    omit_per_page=True starts in that polite mode (WP remembered Cloudflare shops).
    """
    endpoint = _endpoint(base_url)
    polite = bool(omit_per_page) or per_page in (None, 0)
    try:
        return _fetch_pages(
            session,
            endpoint=endpoint,
            per_page=None if polite else (int(per_page) if per_page else 50),
            delay_seconds=delay_seconds,
        )
    except RuntimeError as exc:
        if polite or "HTTP 403" not in str(exc):
            raise
        print("  Woo HTTP 403 with per_page; retrying polite paging (page only)", flush=True)
        return _fetch_pages(
            session,
            endpoint=endpoint,
            per_page=None,
            delay_seconds=delay_seconds,
        )
