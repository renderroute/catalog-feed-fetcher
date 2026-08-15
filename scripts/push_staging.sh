#!/usr/bin/env bash
# Merge catalog JSON from .out into the private staging branch (does not wipe other shops).
set -euo pipefail

gh_out() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1" >> "$GITHUB_OUTPUT"
  fi
}

if [ -z "${STAGING_PUSH_TOKEN:-}" ] || [ -z "${STAGING_REPO:-}" ]; then
  echo "Skipping staging push (STAGING_PUSH_TOKEN / STAGING_REPO not set)."
  gh_out "pushed=false"
  exit 0
fi

SRC="${GITHUB_WORKSPACE}/.out"
CATALOG_COUNT=$(find "$SRC" -maxdepth 1 -type f -name '*.json' ! -name '_*' | wc -l | tr -d ' ')
FAIL_COUNT=$(find "$SRC" -maxdepth 1 -type f -name '_failures.json' | wc -l | tr -d ' ')
if [ "$CATALOG_COUNT" = "0" ] && [ "$FAIL_COUNT" = "0" ]; then
  echo "Nothing to stage (no catalogs, no failure report)."
  gh_out "pushed=false"
  exit 0
fi

REPO="$(printf '%s' "$STAGING_REPO" | tr -d '\r\n')"
REPO="${REPO#"${REPO%%[![:space:]]*}"}"
REPO="${REPO%"${REPO##*[![:space:]]}"}"
REPO="${REPO#https://github.com/}"
REPO="${REPO#git@github.com:}"
REPO="${REPO%.git}"
REPO="${REPO%/}"

BRANCH="$(printf '%s' "${STAGING_BRANCH:-}" | tr -d '\r\n')"
BRANCH="${BRANCH#"${BRANCH%%[![:space:]]*}"}"
BRANCH="${BRANCH%"${BRANCH##*[![:space:]]}"}"
if [ -z "$BRANCH" ]; then
  BRANCH=catalog-incoming
fi

if ! printf '%s' "$REPO" | grep -Eq '^[^/[:space:]]+/[^/[:space:]]+$'; then
  echo "STAGING_REPO must be owner/name (e.g. myuser/my-private-staging)."
  exit 1
fi

BASIC="$(printf 'x-access-token:%s' "$STAGING_PUSH_TOKEN" | base64 | tr -d '\n')"
GIT_AUTH=(-c "http.https://github.com/.extraheader=AUTHORIZATION: basic ${BASIC}")

echo "Staging merge: github.com/${REPO} branch=${BRANCH} catalogs=${CATALOG_COUNT}"
DAY=$(date -u +%Y-%m-%d)
WORK=/tmp/staging-push

for attempt in 1 2 3 4 5 6; do
  rm -rf "$WORK"
  git "${GIT_AUTH[@]}" clone --depth 1 "https://github.com/${REPO}.git" "$WORK"
  cd "$WORK"
  git config user.name "catalog-bot"
  git config user.email "catalog-bot@users.noreply.github.com"

  if git "${GIT_AUTH[@]}" ls-remote --heads origin "$BRANCH" | grep -q "$BRANCH"; then
    git "${GIT_AUTH[@]}" fetch --depth 1 origin "$BRANCH"
    git checkout -B "$BRANCH" FETCH_HEAD
  else
    git checkout -B "$BRANCH"
  fi

  mkdir -p "catalog-bridge/incoming/${DAY}"
  mkdir -p "catalog-bridge/incoming/latest"

  # Merge: copy this batch only. Do not delete other shops already in latest/.
  find "$SRC" -maxdepth 1 -type f -name '*.json' ! -name '_fetch_status.json' ! -name '_slow_queue.json' \
    ! -name '_bridge_keys.txt' \
    \( -name '_failures.json' -o ! -name '_*' \) \
    -exec cp -f {} "catalog-bridge/incoming/${DAY}/" \;
  find "$SRC" -maxdepth 1 -type f -name '*.json' ! -name '_fetch_status.json' ! -name '_slow_queue.json' \
    ! -name '_bridge_keys.txt' \
    \( -name '_failures.json' -o ! -name '_*' \) \
    -exec cp -f {} "catalog-bridge/incoming/latest/" \;

  git add -f catalog-bridge/incoming
  if git diff --cached --quiet; then
    echo "No catalog changes to commit"
    gh_out "pushed=true"
    exit 0
  fi
  git commit -m "catalog incoming ${DAY}"
  if git "${GIT_AUTH[@]}" push -u origin "HEAD:${BRANCH}"; then
    gh_out "pushed=true"
    exit 0
  fi
  echo "Push conflict (attempt ${attempt}); retrying after pull..."
  sleep $((attempt * 3))
done
echo "Failed to push staging after retries"
exit 1
