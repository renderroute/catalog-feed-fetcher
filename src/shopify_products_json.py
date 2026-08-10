from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin

import requests

from . import USER_AGENT, compose_title, money


def fetch_shopify_products_json(
    session: requests.Session,
    *,
    base_url: str,
    page_size: int = 100,
    delay_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    # Prefer collections/all; also try /products.json if needed by caller.
    endpoint = urljoin(base_url.rstrip("/") + "/", "collections/all/products.json")
    product_base = base_url.rstrip("/") + "/products/"
    page = 1
    items: list[dict[str, Any]] = []

    while True:
        resp = session.get(
            endpoint,
            params={"limit": int(page_size), "page": page},
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=90,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After") or "60")
            raise RuntimeError(f"Shopify products.json HTTP 429 (retry_after={retry_after})")
        resp.raise_for_status()
        payload = resp.json()
        products = payload.get("products") if isinstance(payload, dict) else None
        if not isinstance(products, list):
            raise RuntimeError("Unexpected products.json shape")
        if page == 1 and len(products) == 0:
            break

        for product in products:
            if not isinstance(product, dict):
                continue
            handle = str(product.get("handle") or "").strip()
            product_title = str(product.get("title") or "").strip()
            vendor = str(product.get("vendor") or "").strip()
            product_type = str(product.get("product_type") or "").strip()
            product_url = product_base + handle if handle else ""
            product_image = ""
            images = product.get("images") or []
            if images and isinstance(images[0], dict):
                product_image = str(images[0].get("src") or "")

            for variant in product.get("variants") or []:
                if not isinstance(variant, dict):
                    continue
                variant_id = str(variant.get("id") or "").strip()
                if not variant_id:
                    continue
                current = money(variant.get("price"))
                compare = money(variant.get("compare_at_price"))
                list_price = current
                sale_price = None
                if current is not None and compare is not None and compare > current:
                    list_price = compare
                    sale_price = current
                available = variant.get("available")
                availability = (
                    "in_stock"
                    if available is True
                    else ("out_of_stock" if available is False else "")
                )
                vtitle = str(variant.get("title") or "").strip()
                image_url = ""
                feat = variant.get("featured_image")
                if isinstance(feat, dict):
                    image_url = str(feat.get("src") or "")
                if not image_url:
                    image_url = product_image
                variant_url = f"{product_url}?variant={variant_id}" if product_url else ""
                items.append(
                    {
                        "merchant_item_id": variant_id,
                        "title": compose_title(product_title, vtitle),
                        "product_url": variant_url,
                        "image_url": image_url,
                        "product_type": product_type,
                        "brand": vendor,
                        "mpn": str(variant.get("sku") or "").strip(),
                        "gtin": str(variant.get("barcode") or "").strip(),
                        "availability": availability,
                        "price": list_price,
                        "sale_price": sale_price,
                        "effective_price": sale_price if sale_price is not None else list_price,
                        "feed_condition": "new",
                    }
                )

        if len(products) < int(page_size):
            break
        page += 1
        time.sleep(max(0.0, float(delay_seconds)))

    return items
