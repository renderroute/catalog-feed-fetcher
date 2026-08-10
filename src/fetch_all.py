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


def fetch_store(session: requests.Session, store: dict, defaults: dict) -> tuple[str, list]:
    platform = (store.get("platform") or "").strip().lower()
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch public storefront catalogs")
    parser.add_argument("--config", default=str(ROOT / "stores.yml"))
    parser.add_argument("--out-dir", default=str(ROOT / ".out"))
    parser.add_argument("--store", action="append", default=[], help="Only these retailer keys")
    parser.add_argument("--dry-run", action="store_true", help="Fetch but do not write files")
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    defaults = cfg.get("defaults") or {}
    stores = cfg.get("stores") or []
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
