from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests
import yaml

from . import build_envelope
from .shopify_graphql import fetch_shopify_graphql
from .shopify_products_json import fetch_shopify_products_json
from .woocommerce_store_api import fetch_woocommerce_store_api

ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def normalize_platform(platform: str) -> str:
    platform = (platform or "").strip().lower()
    aliases = {
        "shopify_storefront_graphql": "shopify",
        "shopify_graphql": "shopify",
        "shopify_products_json": "shopify_products_json",
        "products_json": "shopify_products_json",
        "woocommerce_store_api": "woocommerce",
        "woo": "woocommerce",
    }
    return aliases.get(platform, platform)


def fetch_store(session: requests.Session, store: dict, defaults: dict) -> tuple[str, list]:
    platform = normalize_platform(str(store.get("platform") or ""))
    base_url = (store.get("base_url") or "").strip()
    delay = float(store.get("delay_seconds", defaults.get("delay_seconds", 2.0)))
    if not base_url:
        raise ValueError("base_url required")

    if platform == "shopify":
        version = str(store.get("shopify_graphql_version", defaults.get("shopify_graphql_version", "2024-07")))
        page_size = int(store.get("shopify_page_size", defaults.get("shopify_page_size", 100)))
        try:
            items = fetch_shopify_graphql(
                session,
                base_url=base_url,
                version=version,
                page_size=page_size,
                delay_seconds=delay,
            )
            return "shopify_storefront_graphql", items
        except Exception as exc:
            print(f"  GraphQL failed ({exc}); falling back to products.json", file=sys.stderr)
            items = fetch_shopify_products_json(
                session,
                base_url=base_url,
                page_size=min(page_size, 100),
                delay_seconds=max(delay, 2.0),
            )
            return "shopify_products_json", items

    if platform == "shopify_products_json":
        page_size = int(store.get("shopify_page_size", defaults.get("shopify_page_size", 100)))
        items = fetch_shopify_products_json(
            session,
            base_url=base_url,
            page_size=min(page_size, 100),
            delay_seconds=max(delay, 2.0),
        )
        return "shopify_products_json", items

    if platform == "woocommerce":
        per_page = int(store.get("woo_per_page", defaults.get("woo_per_page", 50)))
        items = fetch_woocommerce_store_api(
            session,
            base_url=base_url,
            per_page=per_page,
            delay_seconds=delay,
        )
        return "woocommerce_store_api", items

    raise ValueError(f"Unsupported platform: {platform}")


def stores_from_inline(args: argparse.Namespace) -> list[dict]:
    key = (args.inline_store or "").strip()
    base_url = (args.base_url or "").strip()
    platform = normalize_platform(args.platform or "")
    if not key or not base_url or not platform:
        return []
    return [
        {
            "key": key,
            "base_url": base_url,
            "platform": platform,
            "enabled": True,
        }
    ]


def stores_from_json(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("stores"), list):
        raw = raw["stores"]
    if not isinstance(raw, list):
        raise ValueError("stores JSON must be a list or {stores: [...]}")
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or row.get("retailer_key") or "").strip()
        base_url = str(row.get("base_url") or "").strip().rstrip("/")
        platform = normalize_platform(str(row.get("platform") or row.get("feed_format") or ""))
        if not key or not base_url or not platform:
            continue
        out.append(
            {
                "key": key,
                "base_url": base_url,
                "platform": platform,
                "enabled": bool(row.get("enabled", True)),
                "delay_seconds": row.get("delay_seconds"),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch public storefront catalogs")
    parser.add_argument("--config", default=str(ROOT / "stores.yml"))
    parser.add_argument("--out-dir", default=str(ROOT / ".out"))
    parser.add_argument("--store", action="append", default=[], help="Only these retailer keys (stores.yml mode)")
    parser.add_argument("--stores-json", default="", help="Dynamic store list JSON file (preferred over stores.yml)")
    parser.add_argument("--inline-store", default="", help="Single dynamic retailer key (with --base-url --platform)")
    parser.add_argument("--base-url", default="", help="Single dynamic store base URL")
    parser.add_argument("--platform", default="", help="shopify | shopify_products_json | woocommerce")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but do not write files")
    args = parser.parse_args(argv)

    defaults: dict = {}
    stores: list[dict] = []

    inline = stores_from_inline(args)
    if inline:
        stores = inline
        print("mode=inline dynamic store (no stores.yml)")
    elif (args.stores_json or "").strip():
        stores = stores_from_json(Path(args.stores_json))
        print(f"mode=stores-json count={len(stores)}")
    else:
        cfg = load_config(Path(args.config))
        defaults = cfg.get("defaults") or {}
        stores = cfg.get("stores") or []
        print("mode=stores.yml (legacy / backup)")

    only = set(args.store)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    ok = 0
    failed = 0

    for store in stores:
        key = str(store.get("key") or "").strip()
        if not key:
            continue
        if only and key not in only:
            continue
        if not store.get("enabled", True):
            print(f"skip {key} (disabled)")
            continue

        print(f"fetch {key} ...")
        try:
            feed_format, items = fetch_store(session, store, defaults)
            envelope = build_envelope(
                retailer_key=key,
                feed_format=feed_format,
                items=items,
            )
            print(f"  ok format={feed_format} items={len(items)}")
            if not args.dry_run:
                path = out_dir / f"{key}.json"
                path.write_text(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                print(f"  wrote {path}")
            ok += 1
        except Exception as exc:
            failed += 1
            print(f"  FAIL {key}: {exc}", file=sys.stderr)

    print(f"done ok={ok} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
