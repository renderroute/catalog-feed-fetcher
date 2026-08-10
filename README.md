# catalog-feed-fetcher

Generic scheduled fetcher for **public** storefront catalog JSON (Shopify Storefront GraphQL, Shopify `products.json`, WooCommerce Store API).

This repository is intentionally **anonymous**. It does not document, name, or call any downstream commercial website.

## What it does

1. Reads `stores.yml`
2. Fetches catalogs politely (sequential, delays, retries)
3. Writes lean JSON (`schema_version: pcpmf_public_catalog_v1`)
4. Optionally pushes those files to a **private** staging repository (write-only token)

## What it must never do

- Mention or call any production website ingest API
- Embed production API secrets
- Use a brand-identifying User-Agent

## Local run

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m src.fetch_all --store elitehubs
```

## GitHub Actions

See `.github/workflows/fetch.yml`.

Required secret (optional until staging is wired):

- `STAGING_PUSH_TOKEN` — fine-grained PAT with **contents: write** only to the private staging repo path
- `STAGING_REPO` — `owner/name` of the private staging repo
- `STAGING_BRANCH` — usually `main`
