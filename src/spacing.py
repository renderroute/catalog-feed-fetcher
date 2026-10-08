"""Spread shops out so one runner never hits several shops on the same host back to back.

Hosts are guessed from a DNS lookup only (no request reaches the shop). Shops whose
addresses share the same first two IPv4 parts are treated as one host group.
"""

from __future__ import annotations

import os
import random
import socket
import threading
import time
from urllib.parse import urlparse

SHOPIFY_GROUP = "shopify"
SHOPIFY_PLATFORMS = {"shopify", "shopify_products_json"}

# Gap before each shop (seconds).
GAP_DEFAULT = (20.0, 60.0)
GAP_SHOPIFY = (3.0, 10.0)
# Minimum spacing after the previous shop on the same (non-Shopify) host group.
GAP_SAME_GROUP = (60.0, 180.0)
SAME_GROUP_WINDOW = 15 * 60.0


def spacing_enabled() -> bool:
    flag = (os.environ.get("CATALOG_FETCH_SPACING") or "on").strip().lower()
    return flag not in ("0", "off", "false", "no")


def _hostname(base_url: str) -> str:
    raw = (base_url or "").strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    return (urlparse(raw).hostname or "").lower()


def host_group(store: dict) -> str:
    platform = str(store.get("platform") or "").strip().lower()
    if platform in SHOPIFY_PLATFORMS:
        return SHOPIFY_GROUP
    host = _hostname(str(store.get("base_url") or ""))
    if not host:
        return "unknown"
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return f"host:{host}"
    ipv4 = sorted({info[4][0] for info in infos if info[0] == socket.AF_INET})
    if ipv4:
        parts = ipv4[0].split(".")
        return f"net4:{parts[0]}.{parts[1]}"
    ipv6 = sorted({info[4][0] for info in infos if info[0] == socket.AF_INET6})
    if ipv6:
        parts = ipv6[0].split(":")
        return f"net6:{parts[0]}:{parts[1] if len(parts) > 1 else ''}"
    return f"host:{host}"


def order_stores(stores: list[dict]) -> list[tuple[dict, str]]:
    """Random order where the same host group is not fetched twice in a row when avoidable."""
    buckets: dict[str, list[dict]] = {}
    for store in stores:
        buckets.setdefault(host_group(store), []).append(store)
    for items in buckets.values():
        random.shuffle(items)

    ordered: list[tuple[dict, str]] = []
    last = ""
    while any(buckets.values()):
        choices = [g for g, items in buckets.items() if items and g != last]
        if not choices:
            choices = [g for g, items in buckets.items() if items]
        most = max(len(buckets[g]) for g in choices)
        group = random.choice([g for g in choices if len(buckets[g]) == most])
        ordered.append((buckets[group].pop(), group))
        last = group
    return ordered


class Spacer:
    """Thread-safe: remembers when each host group was last used and holds one shop per group at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._group_locks: dict[str, threading.Lock] = {}
        self._last_used: dict[str, float] = {}
        self._first = True

    def group_lock(self, group: str) -> threading.Lock:
        with self._lock:
            if group not in self._group_locks:
                self._group_locks[group] = threading.Lock()
            return self._group_locks[group]

    def wait_before(self, group: str) -> float:
        """Sleep before starting a shop in this group; returns seconds slept."""
        if not spacing_enabled():
            return 0.0
        with self._lock:
            if self._first:
                self._first = False
                return 0.0
            if group == SHOPIFY_GROUP:
                wait = random.uniform(*GAP_SHOPIFY)
            else:
                wait = random.uniform(*GAP_DEFAULT)
                last = self._last_used.get(group)
                if last is not None and time.monotonic() - last < SAME_GROUP_WINDOW:
                    since = time.monotonic() - last
                    wait = max(wait, random.uniform(*GAP_SAME_GROUP) - since)
        if wait > 0:
            time.sleep(wait)
        return max(0.0, wait)

    def mark_used(self, group: str) -> None:
        with self._lock:
            self._last_used[group] = time.monotonic()
