# catalog-feed-fetcher

Generic scheduled / dispatched fetcher for **public** storefront catalog JSON
(Shopify Storefront GraphQL, Shopify `products.json`, WooCommerce Store API).

This repository is intentionally **anonymous**. It does not document, name, or
call any downstream commercial website.

## What it does

1. Accepts a **dynamic** store list (preferred) via:
   - `workflow_dispatch` inputs: `store` + `base_url` + `platform`
   - `repository_dispatch` type `fetch-catalogs` with `client_payload.stores`
2. Or falls back to `stores.yml` when only a store key is provided (legacy)
3. Fetches catalogs politely (sequential, delays, retries)
4. Writes lean JSON (`schema_version: pcpmf_public_catalog_v1`)
5. Optionally pushes those files to a **private** staging repository
6. Optionally triggers a private “bridge” workflow to consume staged JSON

## What it must never do

- Mention or call any production website ingest API
- Embed production API secrets / Bearer tokens for a live site
- Use a brand-identifying User-Agent

## Local run

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Dynamic single store (production-style)
python -m src.fetch_all --out-dir .out \
  --inline-store example --base-url https://example.com --platform shopify

# Legacy stores.yml
python -m src.fetch_all --store elitehubs
```

## GitHub Actions

See `.github/workflows/fetch.yml`.

### Staging push (optional)

- `STAGING_PUSH_TOKEN` — fine-grained PAT with **contents: write** on the private staging repo
- `STAGING_REPO` — `owner/name`
- `STAGING_BRANCH` — usually `catalog-incoming`

### Bridge trigger (optional)

- `BRIDGE_DISPATCH_TOKEN` — PAT with **actions: write** on the private bridge repo
- `BRIDGE_REPO` — `owner/name`
- `BRIDGE_WORKFLOW` — workflow file name (default `catalog-bridge.yml`)
- `BRIDGE_REF` — git ref (default `main`)
