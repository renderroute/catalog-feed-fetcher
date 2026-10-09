"""robots.txt check before fetching a store (RFC 9309 matching).

Stores marked identify=true (the shop gave permission) skip the check.
Rules: our product-token group first, else the "*" group; longest matching
pattern wins, Allow wins a tie; "*" and "$" wildcards supported.
4xx robots.txt = allowed; 5xx / network error = not allowed for this run.
"""

from __future__ import annotations

import re
import threading
from urllib.parse import urljoin, urlparse

import requests

from . import user_agent_for

_CACHE: dict[str, tuple[int, str]] = {}
_CACHE_LOCK = threading.Lock()
MAX_BYTES = 500 * 1024


class RobotsDisallowed(RuntimeError):
    pass


def product_token(user_agent: str) -> str:
    return (user_agent or "").split("/", 1)[0].strip().lower()


def parse_groups(body: str) -> list[tuple[list[str], list[tuple[bool, str]]]]:
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    last_was_agent = False
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = line.split(":", 1)
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if not last_was_agent and (agents or rules):
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            last_was_agent = True
        elif field in ("allow", "disallow"):
            last_was_agent = False
            if agents and value:
                rules.append((field == "allow", value))
        else:
            last_was_agent = False
    if agents:
        groups.append((agents, rules))
    return groups


def _pattern_regex(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in core)
    return re.compile("^" + regex + ("$" if anchored else ""))


def _agent_is_ours(agent: str, token: str) -> bool:
    """Our full token, or any agent starting with our brand (text before the first "-")."""
    if not agent or agent == "*":
        return False
    if token.startswith(agent):
        return True
    brand = token.split("-", 1)[0]
    return "-" in token and len(brand) >= 4 and agent.startswith(brand)


def is_allowed(body: str, token: str, path: str) -> bool:
    token = (token or "").lower()
    groups = parse_groups(body)
    mine = [r for agents, rules in groups for r in rules if any(_agent_is_ours(a, token) for a in agents)]
    matched_named = any(any(_agent_is_ours(a, token) for a in agents) for agents, _ in groups)
    if not matched_named:
        mine = [r for agents, rules in groups for r in rules if "*" in agents]
    best_len = -1
    best_allow = True
    for allow, pattern in mine:
        if _pattern_regex(pattern).match(path):
            length = len(pattern)
            if length > best_len or (length == best_len and allow):
                best_len = length
                best_allow = allow
    return best_allow


def _robots_body(session: requests.Session, origin: str, user_agent: str) -> tuple[int, str]:
    with _CACHE_LOCK:
        if origin in _CACHE:
            return _CACHE[origin]
    url = origin + "/robots.txt"
    try:
        resp = session.get(url, headers={"User-Agent": user_agent}, timeout=20)
    except requests.RequestException as exc:
        raise RobotsDisallowed(f"robots.txt check failed: {exc}") from exc
    status = int(resp.status_code)
    body = resp.text[:MAX_BYTES] if 200 <= status < 300 else ""
    if status >= 500:
        raise RobotsDisallowed(f"robots.txt check failed with HTTP {status}")
    with _CACHE_LOCK:
        _CACHE[origin] = (status, body)
    return status, body


def fetch_paths(store: dict) -> list[str]:
    """URLs (path + query) the fetcher will request, primary first."""
    platform = str(store.get("platform") or "")
    base = (store.get("base_url") or "").strip().rstrip("/")
    if platform == "google_xml":
        p = urlparse(base)
        return [(p.path or "/") + (("?" + p.query) if p.query else "")]
    if platform == "woocommerce":
        if "/wp-json/wc/store" in base:
            path = urlparse(base.split("?")[0]).path
        else:
            path = urlparse(urljoin(base + "/", "wp-json/wc/store/v1/products")).path
        return [path + "?per_page=50&page=1"]
    version = str(store.get("shopify_graphql_version") or "2025-10")
    products_json = "/collections/all/products.json?limit=100&page=1"
    if platform == "shopify":
        return [f"/api/{version}/graphql.json", products_json]
    return [products_json]


def check_store(session: requests.Session, store: dict) -> dict:
    """Raise RobotsDisallowed when the shop's robots.txt asks us not to fetch.

    Returns {"products_json_ok": bool} so the Shopify fallback respects robots too.
    """
    if store.get("identify"):
        return {"products_json_ok": True}
    base = (store.get("base_url") or "").strip()
    p = urlparse(base)
    if not p.scheme or not p.netloc:
        return {"products_json_ok": True}
    origin = f"{p.scheme}://{p.netloc}"
    user_agent = user_agent_for(store)
    status, body = _robots_body(session, origin, user_agent)
    if status >= 400 or not body.strip():
        return {"products_json_ok": True}
    token = product_token(user_agent)
    paths = fetch_paths(store)
    if not is_allowed(body, token, paths[0]):
        raise RobotsDisallowed(f"Skipped: robots.txt asks crawlers not to fetch {paths[0].split('?')[0]}")
    products_json_ok = True
    if len(paths) > 1:
        products_json_ok = is_allowed(body, token, paths[1])
    return {"products_json_ok": products_json_ok}
