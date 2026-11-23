"""Fetch relay for shops that block GitHub's addresses.

Some shops return HTTP 403 to GitHub runners (cloud addresses) but answer
normally from Cloudflare's network. Our own Cloudflare Worker (code in the
private repo) repeats such requests from there and returns the shop's reply
unchanged.

Two ways a shop goes through the relay:
  1. Always: hosts in FETCH_RELAY_HOSTS (known blockers). Never tried direct.
  2. Backup: any other shop. Tried direct first; on HTTP 403 the same request
     is sent once through the relay, and the rest of that shop's requests in
     this run go through the relay too. If the relay also gets 403, that reply
     is returned and the shop fails as before. Same crawler name either way,
     so a shop that blocks us by name stays blocked; robots.txt is still read
     and obeyed by the caller.

Settings (repo secrets, never in code):
  FETCH_RELAY_URL       Worker address
  FETCH_RELAY_KEY       shared secret the Worker checks
  FETCH_RELAY_HOSTS     optional comma list of always-relay hosts
  FETCH_RELAY_FALLBACK  optional "off" to disable the 403 backup
URL or key empty = relay fully off, every shop goes direct as before.
"""

from __future__ import annotations

import os
import sys
import threading
from urllib.parse import urlparse

import requests
from requests.adapters import BaseAdapter, HTTPAdapter

_LOCK = threading.Lock()
# Hosts that fell back to the relay during this run (process wide, so a new
# session per page still goes straight to the relay after the first 403).
_BACKUP_HOSTS: set[str] = set()


def _norm_host(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _host_of(url: str) -> str:
    return _norm_host(urlparse(url if "://" in url else "https://" + url).hostname or "")


def _always_hosts() -> list[str]:
    raw = os.environ.get("FETCH_RELAY_HOSTS") or ""
    return [h for h in (_norm_host(p) for p in raw.split(",")) if h]


def relay_settings() -> tuple[str, str]:
    url = (os.environ.get("FETCH_RELAY_URL") or "").strip()
    key = (os.environ.get("FETCH_RELAY_KEY") or "").strip()
    if not url or not key:
        return "", ""
    return url, key


def relay_enabled() -> bool:
    return bool(relay_settings()[0])


def fallback_enabled() -> bool:
    flag = (os.environ.get("FETCH_RELAY_FALLBACK") or "").strip().lower()
    return relay_enabled() and flag not in ("off", "0", "false", "no")


def _matches(host: str, hosts) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def is_relayed(url: str) -> bool:
    """True when this shop is on the always-relay list (and the relay is on)."""
    return relay_enabled() and _matches(_host_of(url), _always_hosts())


def used_relay(url: str) -> str:
    """How this shop was fetched in this run: "relay", "relay_backup" or ""."""
    if is_relayed(url):
        return "relay"
    host = _host_of(url)
    with _LOCK:
        backup = host in _BACKUP_HOSTS
    return "relay_backup" if backup else ""


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


class BackupAdapter(BaseAdapter):
    """Direct first; on HTTP 403 resend once through the relay and keep using it for that shop."""

    def __init__(self, direct: BaseAdapter, relay: RelayAdapter) -> None:
        super().__init__()
        self.direct = direct
        self.relay = relay

    def send(self, request, **kwargs):  # type: ignore[override]
        host = _host_of(request.url)
        with _LOCK:
            sticky = host in _BACKUP_HOSTS
        if sticky:
            return self.relay.send(request, **kwargs)
        resp = self.direct.send(request, **kwargs)
        if resp.status_code != 403:
            return resp
        try:
            relayed = self.relay.send(request, **kwargs)
        except RelayError as exc:
            print(f"  {host}: HTTP 403 direct; fetch relay unavailable ({exc})", file=sys.stderr, flush=True)
            return resp
        except requests.RequestException as exc:
            print(f"  {host}: HTTP 403 direct; fetch relay unreachable ({exc})", file=sys.stderr, flush=True)
            return resp
        with _LOCK:
            first = host not in _BACKUP_HOSTS
            _BACKUP_HOSTS.add(host)
        if first:
            print(
                f"  {host}: HTTP 403 direct; fetch relay (backup) answered HTTP {relayed.status_code}, "
                "using the relay for this shop",
                flush=True,
            )
        if getattr(resp, "raw", None) is not None:
            resp.close()
        return relayed

    def close(self) -> None:
        self.direct.close()
        self.relay.close()


def mount_relay(session: requests.Session) -> bool:
    """Route this session through the relay where needed. Returns True when the relay is on."""
    relay_url, key = relay_settings()
    if not relay_url:
        return False
    relay = RelayAdapter(relay_url, key)
    if fallback_enabled():
        # Wrap whatever adapter the session already uses (cloudscraper keeps its own TLS setup).
        for prefix in ("https://", "http://"):
            session.mount(prefix, BackupAdapter(session.get_adapter(prefix + "x.invalid/"), relay))
    for host in _always_hosts():
        for scheme in ("https", "http"):
            session.mount(f"{scheme}://{host}/", relay)
            session.mount(f"{scheme}://www.{host}/", relay)
    return True
