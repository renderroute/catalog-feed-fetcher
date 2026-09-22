"""Download and normalize a Google Shopping / Merchant Center XML feed."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse

import requests

from . import USER_AGENT, money

# Common Google Merchant / WooCommerce GPF namespaces.
_NS_CANDIDATES = (
    "http://base.google.com/ns/1.0",
    "http://base.google.com/ns/1.0/",
)
_PRICE_CLEAN = re.compile(r"[^0-9.,]+")


def _feed_url(base_url: str) -> str:
    """Accept a full XML URL (preferred) or a bare origin (not useful alone)."""
    raw = (base_url or "").strip()
    if not raw:
        raise ValueError("base_url (full Google XML feed URL) is required")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        raise ValueError("base_url must be an absolute feed URL")
    # Keep path; drop fragment. Query is rare on static XML uploads but allowed.
    return raw.split("#", 1)[0]


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _child_text(el: ET.Element, names: set[str]) -> str:
    for child in list(el):
        if _local(child.tag) in names:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _g_text(el: ET.Element, field: str) -> str:
    """Read g:field or bare field under an <item> (namespace-agnostic)."""
    for child in list(el):
        if _local(child.tag) == field:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    for ns in _NS_CANDIDATES:
        child = el.find(f"{{{ns}}}{field}")
        if child is not None:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""

def _parse_price(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    clean = _PRICE_CLEAN.sub("", raw)
    if not clean:
        return None
    clean = clean.replace(",", "")
    return money(clean)


def _normalize_condition(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in ("used", "refurbished", "renewed"):
        return "used" if value == "used" else value
    if value in ("new", ""):
        return "new"
    return value[:32] or "new"


def _map_item(el: ET.Element) -> dict[str, Any] | None:
    merchant_item_id = _g_text(el, "id")
    if not merchant_item_id:
        return None

    price = _parse_price(_g_text(el, "price"))
    sale_price = _parse_price(_g_text(el, "sale_price"))
    if sale_price is not None and price is not None and sale_price >= price:
        sale_price = None
    effective = sale_price if sale_price is not None else price

    return {
        "merchant_item_id": merchant_item_id,
        "title": _g_text(el, "title") or _child_text(el, {"title"}),
        "product_url": _g_text(el, "link") or _child_text(el, {"link"}),
        "image_url": _g_text(el, "image_link"),
        "product_type": _g_text(el, "product_type"),
        "brand": _g_text(el, "brand"),
        "mpn": _g_text(el, "mpn"),
        "gtin": _g_text(el, "gtin"),
        "ean": _g_text(el, "ean"),
        "upc": _g_text(el, "upc"),
        "availability": _g_text(el, "availability"),
        "feed_condition": _normalize_condition(_g_text(el, "condition")),
        "price": price,
        "sale_price": sale_price,
        "effective_price": effective,
    }


def fetch_google_xml(
    session: requests.Session,
    *,
    base_url: str,
    timeout: int = 300,
) -> list[dict[str, Any]]:
    """
    Download one Google Merchant XML file and return lean catalog items.

    base_url must be the full feed URL (e.g. https://example.com/.../feed.xml).
    """
    url = _feed_url(base_url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml,text/xml,*/*;q=0.8",
    }
    response = session.get(url, headers=headers, timeout=timeout, stream=True)
    if response.status_code == 403:
        raise RuntimeError("Google XML feed HTTP 403 (likely bot/CDN block)")
    if response.status_code != 200:
        raise RuntimeError(f"Google XML feed HTTP {response.status_code}")

    # Stream into ElementTree to avoid holding a 40MB+ string twice when possible.
    response.raw.decode_content = True
    items: list[dict[str, Any]] = []
    try:
        context = ET.iterparse(response.raw, events=("end",))
        for _event, elem in context:
            if _local(elem.tag) != "item":
                continue
            # Only channel-level product items (skip nested oddities).
            mapped = _map_item(elem)
            if mapped:
                items.append(mapped)
            elem.clear()
    except ET.ParseError as exc:
        raise RuntimeError(f"Unable to parse Google XML feed: {exc}") from exc

    if not items:
        # Some feeds put items under a different root; fall back to full parse once.
        response2 = session.get(url, headers=headers, timeout=timeout)
        response2.raise_for_status()
        try:
            root = ET.fromstring(response2.content)
        except ET.ParseError as exc:
            raise RuntimeError(f"Unable to parse Google XML feed: {exc}") from exc
        for elem in root.iter():
            if _local(elem.tag) == "item":
                mapped = _map_item(elem)
                if mapped:
                    items.append(mapped)

    return items
