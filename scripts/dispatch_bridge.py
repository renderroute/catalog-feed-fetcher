#!/usr/bin/env python3
"""Dispatch private Catalog bridge forward. Optional BRIDGE_STORE_KEYS=comma keys."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    if not (os.environ.get("BRIDGE_DISPATCH_TOKEN") or "").strip() or not (os.environ.get("BRIDGE_REPO") or "").strip():
        print("Skipping bridge dispatch (BRIDGE_DISPATCH_TOKEN / BRIDGE_REPO not set).")
        return 0

    repo = (os.environ.get("BRIDGE_REPO") or "").strip()
    repo = repo.replace("https://github.com/", "").removesuffix(".git").strip("/")
    workflow = (os.environ.get("BRIDGE_WORKFLOW") or "catalog-bridge.yml").strip() or "catalog-bridge.yml"
    ref = (os.environ.get("BRIDGE_REF") or "main").strip() or "main"
    token = (os.environ.get("BRIDGE_DISPATCH_TOKEN") or "").strip()
    store = (os.environ.get("BRIDGE_STORE_KEYS") or "").strip()

    body = json.dumps(
        {
            "ref": ref,
            "inputs": {
                "store": store,
                "mode": "apply",
                "local_validate_only": "false",
            },
        }
    ).encode("utf-8")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "CatalogFeedFetcher/0.1",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(
                f"bridge dispatch ok HTTP {resp.status} repo={repo} workflow={workflow} "
                f"store={store or '(all latest)'}"
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"bridge dispatch failed HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
