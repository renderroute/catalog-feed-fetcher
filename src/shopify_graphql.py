from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin

import requests

from . import USER_AGENT, compose_title, money

PRODUCTS_QUERY = """
query CatalogProducts($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        id
        title
        handle
        vendor
        productType
        availableForSale
        priceRange {
          minVariantPrice {
            amount
            currencyCode
          }
        }
        variants(first: 25) {
          pageInfo {
            hasNextPage
            endCursor
          }
          edges {
            node {
              id
              title
              sku
              availableForSale
              price {
                amount
                currencyCode
              }
              compareAtPrice {
                amount
                currencyCode
              }
            }
          }
        }
      }
    }
  }
}
"""


def _gid_numeric(gid: str) -> str:
    # gid://shopify/ProductVariant/123 -> 123
    if not gid:
        return ""
    return str(gid).rstrip("/").split("/")[-1]


def graphql_endpoint(base_url: str, version: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", f"api/{version}/graphql.json")


def fetch_shopify_graphql(
    session: requests.Session,
    *,
    base_url: str,
    version: str = "2024-07",
    page_size: int = 100,
    delay_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    endpoint = graphql_endpoint(base_url, version)
    product_base = base_url.rstrip("/") + "/products/"
    cursor = None
    items: list[dict[str, Any]] = []

    while True:
        payload = {
            "query": PRODUCTS_QUERY,
            "variables": {"first": int(page_size), "after": cursor},
        }
        resp = session.post(
            endpoint,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=90,
        )
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After") or "60")
            raise RuntimeError(f"Shopify GraphQL HTTP 429 (retry_after={retry_after})")
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            msgs = "; ".join(
                str(e.get("message") or e) for e in data["errors"] if isinstance(e, dict)
            )
            raise RuntimeError(f"Shopify GraphQL errors: {msgs}")

        products = (((data.get("data") or {}).get("products")) or {})
        edges = products.get("edges") or []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            handle = (node.get("handle") or "").strip()
            product_title = (node.get("title") or "").strip()
            vendor = (node.get("vendor") or "").strip()
            product_type = (node.get("productType") or "").strip()
            product_url = product_base + handle if handle else ""

            variant_edges = (((node.get("variants") or {}).get("edges")) or [])
            # Note: variants beyond first 25 are skipped in v1 (rare for PC parts).
            for vedge in variant_edges:
                vnode = (vedge or {}).get("node") or {}
                variant_id = _gid_numeric(str(vnode.get("id") or ""))
                if not variant_id:
                    continue
                price = money((vnode.get("price") or {}).get("amount"))
                compare = money((vnode.get("compareAtPrice") or {}).get("amount"))
                list_price = price
                sale_price = None
                if price is not None and compare is not None and compare > price:
                    list_price = compare
                    sale_price = price
                available = bool(vnode.get("availableForSale"))
                vtitle = (vnode.get("title") or "").strip()
                variant_url = f"{product_url}?variant={variant_id}" if product_url else ""
                items.append(
                    {
                        "merchant_item_id": variant_id,
                        "title": compose_title(product_title, vtitle),
                        "product_url": variant_url,
                        "image_url": "",
                        "product_type": product_type,
                        "brand": vendor,
                        "mpn": (vnode.get("sku") or "").strip(),
                        "gtin": "",
                        "availability": "in_stock" if available else "out_of_stock",
                        "price": list_price,
                        "sale_price": sale_price,
                        "effective_price": sale_price if sale_price is not None else list_price,
                        "feed_condition": "new",
                    }
                )

        page_info = products.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break
        time.sleep(max(0.0, float(delay_seconds)))

    return items
