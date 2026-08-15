from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

from . import build_envelope
from .shopify_graphql import fetch_shopify_graphql
from .shopify_products_json import fetch_shopify_products_json
from .woocommerce_store_api import WooNeedsCookieRetry, fetch_woocommerce_store_api

ROOT = Path(__file__).resolve().parents[1]
_PRINT_LOCK = threading.Lock()
SLOW_WORKERS = 2


def log(msg: str, *, error: bool = False) -> None:
    with _PRINT_LOCK:
        print(msg, file=sys.stderr if error else sys.stdout, flush=True)


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


def _num(value, fallback):
    """Prefer store value when set; fall back when missing/None/invalid."""
    if value is None or value == "":
        value = fallback
    if value is None or value == "":
        return 0
    return value


def fetch_store(
    session: requests.Session,
    store: dict,
    defaults: dict,
    *,
    cookie_retry: bool = False,
) -> tuple[str, list]:
    platform = normalize_platform(str(store.get("platform") or ""))
    base_url = (store.get("base_url") or "").strip()
    delay = float(_num(store.get("delay_seconds"), defaults.get("delay_seconds", 2.0)) or 2.0)
    if not base_url:
        raise ValueError("base_url required")

    if platform == "shopify":
        version = str(
            store.get("shopify_graphql_version")
            or defaults.get("shopify_graphql_version")
            or "2024-07"
        )
        page_size = int(_num(store.get("shopify_page_size"), defaults.get("shopify_page_size", 100)) or 100)
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
            log(f"  GraphQL failed ({exc}); falling back to products.json", error=True)
            items = fetch_shopify_products_json(
                session,
                base_url=base_url,
                page_size=min(page_size, 100),
                delay_seconds=max(delay, 2.0),
            )
            return "shopify_products_json", items

    if platform == "shopify_products_json":
        page_size = int(_num(store.get("shopify_page_size"), defaults.get("shopify_page_size", 100)) or 100)
        items = fetch_shopify_products_json(
            session,
            base_url=base_url,
            page_size=min(page_size, 100),
            delay_seconds=max(delay, 2.0),
        )
        return "shopify_products_json", items

    if platform == "woocommerce":
        per_page = int(_num(store.get("woo_per_page"), defaults.get("woo_per_page", 50)) or 50)
        items = fetch_woocommerce_store_api(
            session,
            base_url=base_url,
            per_page=per_page,
            delay_seconds=delay,
            omit_per_page=False,
            cookie_retry=cookie_retry,
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
        row_out = {
            "key": key,
            "base_url": base_url,
            "platform": platform,
            "enabled": bool(row.get("enabled", True)),
        }
        if row.get("delay_seconds") is not None and row.get("delay_seconds") != "":
            row_out["delay_seconds"] = row.get("delay_seconds")
        out.append(row_out)
    return out


def _queue_row(store: dict) -> dict:
    row = {
        "key": str(store.get("key") or "").strip(),
        "base_url": str(store.get("base_url") or "").strip(),
        "platform": normalize_platform(str(store.get("platform") or "")),
        "enabled": True,
    }
    if store.get("delay_seconds") is not None and store.get("delay_seconds") != "":
        row["delay_seconds"] = store.get("delay_seconds")
    return row


def _write_status(out_dir: Path, *, ok: int, failed: int, failed_keys: list, failed_rows: list, deferred_keys: list) -> None:
    status_path = out_dir / "_fetch_status.json"
    status_path.write_text(
        json.dumps(
            {
                "ok": ok,
                "failed": failed,
                "failed_keys": failed_keys,
                "failures": failed_rows,
                "deferred_keys": deferred_keys,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    fail_path = out_dir / "_failures.json"
    if failed_rows:
        fail_path.write_text(
            json.dumps({"failures": failed_rows}, ensure_ascii=False),
            encoding="utf-8",
        )
    elif fail_path.exists():
        fail_path.unlink()


def _write_bridge_keys(out_dir: Path, keys: list[str]) -> None:
    (out_dir / "_bridge_keys.txt").write_text(",".join(keys), encoding="utf-8")


def _push_on_finish_enabled() -> bool:
    flag = (os.environ.get("CATALOG_PUSH_ON_FINISH") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    return bool((os.environ.get("STAGING_PUSH_TOKEN") or "").strip())


def _push_finished_store(key: str) -> bool:
    """Stage + dispatch ingest for one finished shop. Serialized by caller."""
    env = os.environ.copy()
    env.setdefault("GITHUB_WORKSPACE", str(ROOT))
    env["BRIDGE_STORE_KEYS"] = key
    bash_script = ROOT / "scripts" / "push_staging.sh"
    dispatch = ROOT / "scripts" / "dispatch_bridge.py"
    log(f"  push-on-finish {key}")
    stage = subprocess.run(
        ["bash", str(bash_script)],
        cwd=str(ROOT),
        env=env,
        timeout=180,
    )
    if stage.returncode != 0:
        log(f"  push-on-finish staging failed {key} rc={stage.returncode}", error=True)
        return False
    if not (env.get("BRIDGE_DISPATCH_TOKEN") or "").strip():
        log(f"  push-on-finish staged {key} (no bridge token)")
        return True
    bridge = subprocess.run(
        [sys.executable, str(dispatch)],
        cwd=str(ROOT),
        env=env,
        timeout=60,
    )
    if bridge.returncode != 0:
        log(f"  push-on-finish bridge failed {key} rc={bridge.returncode}", error=True)
        return False
    log(f"  push-on-finish ok {key}")
    return True
    (out_dir / "_bridge_keys.txt").write_text(",".join(keys), encoding="utf-8")


def _write_catalog(out_dir: Path, key: str, feed_format: str, items: list, dry_run: bool) -> None:
    envelope = build_envelope(retailer_key=key, feed_format=feed_format, items=items)
    log(f"  ok format={feed_format} items={len(items)}")
    if not dry_run:
        path = out_dir / f"{key}.json"
        path.write_text(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        log(f"  wrote {path}")


def _run_one_store(
    store: dict,
    defaults: dict,
    *,
    cookie_retry: bool,
    out_dir: Path,
    dry_run: bool,
) -> tuple[str, str, Exception | None]:
    """Return (key, outcome, error) where outcome is ok|defer|fail."""
    key = str(store.get("key") or "").strip()
    session = requests.Session()
    try:
        feed_format, items = fetch_store(session, store, defaults, cookie_retry=cookie_retry)
        _write_catalog(out_dir, key, feed_format, items, dry_run)
        return key, "ok", None
    except WooNeedsCookieRetry as exc:
        if cookie_retry:
            return key, "fail", exc
        return key, "defer", exc
    except Exception as exc:
        return key, "fail", exc


def _collect_enabled(stores: list[dict], only: set[str]) -> list[dict]:
    out: list[dict] = []
    for store in stores:
        key = str(store.get("key") or "").strip()
        if not key:
            continue
        if only and key not in only:
            continue
        if not store.get("enabled", True):
            log(f"skip {key} (disabled)")
            continue
        out.append(store)
    return out


def run_fast_phase(stores: list[dict], defaults: dict, out_dir: Path, dry_run: bool) -> int:
    enabled = _collect_enabled(stores, set())
    ok = 0
    failed = 0
    failed_keys: list[str] = []
    failed_rows: list[dict] = []
    deferred: list[dict] = []
    bridge_keys: list[str] = []

    log(f"phase=fast stores={len(enabled)}")
    for store in enabled:
        key = str(store.get("key") or "").strip()
        log(f"fetch {key} ...")
        _key, outcome, exc = _run_one_store(
            store, defaults, cookie_retry=False, out_dir=out_dir, dry_run=dry_run
        )
        if outcome == "ok":
            ok += 1
            bridge_keys.append(key)
        elif outcome == "defer":
            log(f"  defer {key} to cookie path (HTTP 403)")
            deferred.append(_queue_row(store))
        else:
            failed += 1
            failed_keys.append(key)
            err = str(exc)
            failed_rows.append(
                {
                    "key": key,
                    "retailer_key": key,
                    "error": err,
                    "http_status": 403 if "403" in err else 0,
                }
            )
            log(f"  FAIL {key}: {exc}", error=True)

    deferred_keys = [str(s["key"]) for s in deferred]
    if not dry_run:
        (out_dir / "_slow_queue.json").write_text(
            json.dumps({"stores": deferred}, ensure_ascii=False),
            encoding="utf-8",
        )
        _write_bridge_keys(out_dir, bridge_keys)
        _write_status(
            out_dir,
            ok=ok,
            failed=failed,
            failed_keys=failed_keys,
            failed_rows=failed_rows,
            deferred_keys=deferred_keys,
        )
    log(f"done phase=fast ok={ok} deferred={len(deferred)} failed={failed}")
    if failed and ok == 0 and not deferred:
        return 1
    return 0


def run_slow_phase(stores: list[dict], defaults: dict, out_dir: Path, dry_run: bool) -> int:
    enabled = _collect_enabled(stores, set())
    prior = {}
    status_path = out_dir / "_fetch_status.json"
    if status_path.exists():
        try:
            prior = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            prior = {}
    ok = int(prior.get("ok") or 0)
    failed = int(prior.get("failed") or 0)
    failed_keys = list(prior.get("failed_keys") or [])
    failed_rows = list(prior.get("failures") or [])
    bridge_keys: list[str] = []
    pushed_keys: list[str] = []
    lock = threading.Lock()
    push_on_finish = _push_on_finish_enabled() and not dry_run

    log(f"phase=slow stores={len(enabled)} workers={SLOW_WORKERS} push_on_finish={push_on_finish}")
    if not enabled:
        if not dry_run:
            _write_bridge_keys(out_dir, [])
        log("done phase=slow ok=0 failed=0")
        return 0

    workers = min(SLOW_WORKERS, len(enabled))
    push_pool = ThreadPoolExecutor(max_workers=1) if push_on_finish else None
    push_futs: list = []

    def record_push(key: str) -> bool:
        did = _push_finished_store(key)
        if did:
            with lock:
                pushed_keys.append(key)
        return did

    def fetch_one(store: dict) -> None:
        nonlocal ok, failed
        key = str(store.get("key") or "").strip()
        log(f"fetch {key} (cookie path) ...")
        _key, outcome, exc = _run_one_store(
            store, defaults, cookie_retry=True, out_dir=out_dir, dry_run=dry_run
        )
        with lock:
            if outcome == "ok":
                ok += 1
                bridge_keys.append(key)
            else:
                failed += 1
                failed_keys.append(key)
                err = str(exc)
                failed_rows.append(
                    {
                        "key": key,
                        "retailer_key": key,
                        "error": err,
                        "http_status": 403 if exc and "403" in err else 0,
                    }
                )
                log(f"  FAIL {key}: {exc}", error=True)
        if outcome == "ok" and push_on_finish and push_pool is not None:
            fut = push_pool.submit(record_push, key)
            with lock:
                push_futs.append(fut)

    with ThreadPoolExecutor(max_workers=workers) as fetch_pool:
        fetch_futs = [fetch_pool.submit(fetch_one, store) for store in enabled]
        for fut in as_completed(fetch_futs):
            fut.result()
    if push_pool is not None:
        for fut in push_futs:
            fut.result()
        push_pool.shutdown(wait=True)

    leftover = [k for k in bridge_keys if k not in pushed_keys]
    if not dry_run:
        _write_bridge_keys(out_dir, leftover)
        _write_status(
            out_dir,
            ok=ok,
            failed=failed,
            failed_keys=failed_keys,
            failed_rows=failed_rows,
            deferred_keys=[],
        )
    log(
        f"done phase=slow batch_ok={len(bridge_keys)} pushed={len(pushed_keys)} "
        f"leftover={len(leftover)} total_ok={ok} failed={failed}"
    )
    if failed and ok == 0:
        return 1
    return 0


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
    parser.add_argument(
        "--phase",
        choices=("fast", "slow", "both"),
        default="both",
        help="fast=quick API only (403 Woo deferred); slow=homepage cookies; both=fast then slow",
    )
    parser.add_argument("--slow-queue", default="", help="JSON file from phase=fast (_slow_queue.json)")
    args = parser.parse_args(argv)

    defaults: dict = {}
    stores: list[dict] = []

    inline = stores_from_inline(args)
    if inline:
        stores = inline
        log("mode=inline dynamic store (no stores.yml)")
    elif (args.stores_json or "").strip():
        stores = stores_from_json(Path(args.stores_json))
        log(f"mode=stores-json count={len(stores)}")
    else:
        cfg = load_config(Path(args.config))
        defaults = cfg.get("defaults") or {}
        stores = cfg.get("stores") or []
        log("mode=stores.yml (legacy / backup)")

    only = set(args.store)
    if only:
        stores = [s for s in stores if str(s.get("key") or "").strip() in only]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.phase == "slow":
        queue_path = Path(args.slow_queue) if (args.slow_queue or "").strip() else (out_dir / "_slow_queue.json")
        if queue_path.exists():
            stores = stores_from_json(queue_path)
            log(f"loaded slow queue count={len(stores)} from {queue_path}")
        else:
            log(f"no slow queue at {queue_path}")
            stores = []
        return run_slow_phase(stores, defaults, out_dir, args.dry_run)

    rc = run_fast_phase(stores, defaults, out_dir, args.dry_run)
    if args.phase == "fast":
        return rc
    queue_path = out_dir / "_slow_queue.json"
    slow_stores: list[dict] = []
    if queue_path.exists():
        slow_stores = stores_from_json(queue_path)
    if not slow_stores:
        return rc
    slow_rc = run_slow_phase(slow_stores, defaults, out_dir, args.dry_run)
    return slow_rc or rc


if __name__ == "__main__":
    raise SystemExit(main())
