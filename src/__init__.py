"""Lean public-catalog JSON schema (generic storefront catalog envelope)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "pcpmf_public_catalog_v1"
USER_AGENT = "CatalogFeedFetcher/0.1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_envelope(
    *,
    retailer_key: str,
    feed_format: str,
    items: list[dict[str, Any]],
    fetched_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "retailer_key": retailer_key,
        "feed_format": feed_format,
        "fetched_at": fetched_at or utc_now_iso(),
        "item_count": len(items),
        "items": items,
    }


def money(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compose_title(product_title: str, variant_title: str) -> str:
    product_title = (product_title or "").strip()
    variant_title = (variant_title or "").strip()
    if not variant_title or variant_title.lower() in ("default title", "default"):
        return product_title
    if not product_title:
        return variant_title
    return f"{product_title} - {variant_title}"
