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


def fetch_woocommerce_store_api(
    session: requests.Session,
    *,
    base_url: str,
    per_page: int = 50,
    delay_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    endpoint = urljoin(base_url.rstrip("/") + "/", "wp-json/wc/store/v1/products")
    page = 1
    items: list[dict[str, Any]] = []

    while True:
        resp = session.get(
            endpoint,
            params={
                "page": page,
                "per_page": int(per_page),
                "_fields": FIELDS,
            },
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
            if not isinstance(product, dict):
                continue
            product_id = str(product.get("id") or "").strip()
            if not product_id:
                continue
            prices = product.get("prices") if isinstance(product.get("prices"), dict) else {}
            # Woo store API amounts are often in minor units as strings.
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
            items.append(
                {
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
            )

        if len(products) < int(per_page):
            break
        page += 1
        time.sleep(max(0.0, float(delay_seconds)))

    return items
