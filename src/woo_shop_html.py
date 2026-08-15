from __future__ import annotations

import html
import re
import time
from typing import Any
from urllib.parse import urljoin

import requests

from . import money

_PRODUCT_ID_RE = re.compile(r'\bdata-id="(\d+)"')
_IMAGE_LINK_RE = re.compile(
    r'href="(https?://[^"]+)"[^>]*class="[^"]*product-image-link[^"]*"[^>]*aria-label="([^"]*)"',
    re.I,
)
_IMAGE_LINK_RE2 = re.compile(
    r'class="[^"]*product-image-link[^"]*"[^>]*href="(https?://[^"]+)"[^>]*aria-label="([^"]*)"',
    re.I,
)
_SKU_RE = re.compile(r'data-product_sku="([^"]*)"')
_IMG_RE = re.compile(r'data-image-url="(https?://[^"]+)"')
_ORIG_PRICE_RE = re.compile(r"Original price was:\s*(?:&#8377;|₹|&\#8377;)?\s*([\d,.]+)", re.I)
_CUR_PRICE_RE = re.compile(r"Current price is:\s*(?:&#8377;|₹|&\#8377;)?\s*([\d,.]+)", re.I)
_AMOUNT_RE = re.compile(r'class="woocommerce-Price-amount[^"]*"[^>]*>.*?<bdi[^>]*>.*?</span>\s*([\d,.]+)', re.I | re.S)


def _price_num(raw: str) -> float | None:
    cleaned = (raw or "").replace(",", "").strip()
    return money(cleaned)


def _parse_shop_cards(page_html: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _PRODUCT_ID_RE.finditer(page_html):
        pid = match.group(1)
        if pid in seen:
            continue
        start = max(0, match.start() - 4000)
        chunk = page_html[start : match.start() + 10000]
        link = _IMAGE_LINK_RE.search(chunk) or _IMAGE_LINK_RE2.search(chunk)
        product_url = html.unescape(link.group(1)).strip() if link else ""
        title = html.unescape(link.group(2)).strip() if link else ""
        sku_m = _SKU_RE.search(chunk)
        img_m = _IMG_RE.search(chunk)
        orig_m = _ORIG_PRICE_RE.search(chunk)
        cur_m = _CUR_PRICE_RE.search(chunk)
        amounts = _AMOUNT_RE.findall(chunk)
        sale = _price_num(cur_m.group(1)) if cur_m else None
        regular = _price_num(orig_m.group(1)) if orig_m else None
        if regular is None and amounts:
            regular = _price_num(amounts[0])
        if sale is None and len(amounts) >= 2:
            sale = _price_num(amounts[1])
        if sale is not None and regular is None:
            regular = sale
            sale = None
        if sale is not None and regular is not None and sale >= regular:
            sale = None
        effective = sale if sale is not None else regular
        if not pid or effective is None:
            continue
        seen.add(pid)
        out_of_stock = bool(re.search(r"\boutofstock\b", chunk[:600], re.I))
        items.append(
            {
                "merchant_item_id": pid,
                "title": title,
                "product_url": product_url,
                "image_url": html.unescape(img_m.group(1)).strip() if img_m else "",
                "product_type": "",
                "brand": "",
                "mpn": html.unescape(sku_m.group(1)).strip() if sku_m else "",
                "gtin": "",
                "availability": "out_of_stock" if out_of_stock else "in_stock",
                "price": regular,
                "sale_price": sale,
                "effective_price": effective,
                "feed_condition": "new",
            }
        )
    return items


def fetch_woocommerce_shop_html(
    session: requests.Session,
    *,
    origin: str,
    delay_seconds: float = 1.0,
) -> list[dict[str, Any]]:
    """Public shop listing pages (used when the Store API stays 403)."""
    base = (origin or "").rstrip("/") + "/"
    shop = urljoin(base, "shop/")
    headers = {
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Referer": base,
    }
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    page = 1
    print(f"  Store API still blocked; reading public shop pages: {shop}", flush=True)
    while page <= 200:
        url = shop if page == 1 else urljoin(shop, f"page/{page}/")
        resp = session.get(url, headers=headers, timeout=90)
        if resp.status_code == 403:
            raise RuntimeError("Woo shop HTML HTTP 403 (likely bot/CDN block)")
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        cards = _parse_shop_cards(resp.text or "")
        new_cards = [c for c in cards if c["merchant_item_id"] not in seen_ids]
        if page == 1 and not cards:
            raise RuntimeError("Woo shop HTML had no products on page 1")
        if not new_cards:
            break
        for card in new_cards:
            seen_ids.add(card["merchant_item_id"])
            items.append(card)
        print(f"  shop page {page}: +{len(new_cards)} (total {len(items)})", flush=True)
        page += 1
        time.sleep(max(0.0, float(delay_seconds)))
    if not items:
        raise RuntimeError("Woo shop HTML returned no products")
    return items
