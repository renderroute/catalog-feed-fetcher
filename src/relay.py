"""Optional fetch relay for shops that block GitHub's addresses.

A few shops return HTTP 403 to every GitHub runner but answer normally from
Cloudflare's network. For those hosts only, requests go through our own
Cloudflare Worker, which makes the same request and returns the shop's reply
unchanged (the Worker code lives in the private repo).

All settings come from repo secrets, never from code:
  FETCH_RELAY_URL    Worker address, e.g. https://name.account.workers.dev/
  FETCH_RELAY_KEY    shared secret the Worker checks
  FETCH_RELAY_HOSTS  comma list of shop hosts to relay, e.g. nclcomputer.com,gamesncomps.com
Any of them empty = relay off, every shop goes direct as before.

Works by mounting a transport adapter on the session for those hosts only, so
the fetch code (pages, robots.txt, redirects, cookies) does not change.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter


def _hosts() -> list[str]:
    raw = os.environ.get("FETCH_RELAY_HOSTS") or ""
    out = []
    for part in raw.split(","):
        host = part.strip().lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            out.append(host)
    return out


def relay_settings() -> tuple[str, str, list[str]]:
    url = (os.environ.get("FETCH_RELAY_URL") or "").strip()
    key = (os.environ.get("FETCH_RELAY_KEY") or "").strip()
    hosts = _hosts()
    if not url or not key or not hosts:
        return "", "", []
    return url, key, hosts


def is_relayed(url: str) -> bool:
    _relay_url, _key, hosts = relay_settings()
    if not hosts:
        return False
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


class RelayError(RuntimeError):
    """The relay itself refused the call (bad key, host not allowed). Not a shop block."""


class RelayAdapter(HTTPAdapter):
    def __init__(self, relay_url: str, key: str) -> None:
        super().__init__()
        self.relay_url = relay_url
        self.key = key

    def send(self, request, **kwargs):  # type: ignore[override]
        target = request.url
        relayed = request.copy()
        relayed.url = self.relay_url
        relayed.headers["X-Relay-Target"] = target
        relayed.headers["X-Relay-Key"] = self.key
        resp = super().send(relayed, **kwargs)
        if resp.headers.get("X-Relay-Error"):
            raise RelayError(f"Fetch relay refused the request (HTTP {resp.status_code}): {resp.text.strip()[:200]}")
        # Look like a normal reply from the shop: redirects, cookies and logs use the shop URL.
        resp.url = target
        resp.request = request
        return resp


def mount_relay(session: requests.Session) -> bool:
    """Route the relay hosts on this session through the Worker. Returns True when the relay is on."""
    relay_url, key, hosts = relay_settings()
    if not hosts:
        return False
    adapter = RelayAdapter(relay_url, key)
    for host in hosts:
        for scheme in ("https", "http"):
            session.mount(f"{scheme}://{host}/", adapter)
            session.mount(f"{scheme}://www.{host}/", adapter)
    return True
