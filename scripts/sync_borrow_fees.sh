#!/bin/bash
# Pull the "IBKR borrow-fee snapshot" workflow artifacts (kept 90 days on GitHub)
# into research_cache/borrow_fees/raw/ so they outlive the retention window.
# Already-synced runs are skipped (listed in synced_runs.txt). Needs `gh` logged in.
#   bash scripts/sync_borrow_fees.sh [OUT_DIR]
set -euo pipefail
REPO=superdiaodiao/quant_stocks
OUT=${1:-/Users/bytedance/code/quant_stocks/research_cache/borrow_fees}
mkdir -p "$OUT/raw"
touch "$OUT/synced_runs.txt"
gh api "repos/$REPO/actions/artifacts?per_page=100" --paginate \
  --jq '.artifacts[] | select(.name | startswith("borrow-fees-")) | select(.expired | not) | .name' |
while read -r name; do
  grep -qx "$name" "$OUT/synced_runs.txt" && continue
  tmp=$(mktemp -d)
  if gh run download "${name#borrow-fees-}" -R "$REPO" -n "$name" -D "$tmp" >/dev/null 2>&1; then
    for f in "$tmp"/*.txt.gz; do
      [ -e "$f" ] && cp -n "$f" "$OUT/raw/gh_$(basename "$f")"
    done
    echo "$name" >> "$OUT/synced_runs.txt"
    echo "synced $name"
  else
    echo "failed $name" >&2
  fi
  rm -rf "$tmp"
done
