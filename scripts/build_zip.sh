#!/usr/bin/env bash
# ============================================================
#  Market Refinement Dashboard - reproducible release packager
# ============================================================
#  Produces dist/market-refinement-dashboard.zip.
#
#  What gets INCLUDED:
#    - Source code:  app/  frontend/  tests/  scripts/
#    - Launchers:    start.bat  start.sh  verify_startup.py
#    - Configs/docs: requirements.txt  pytest.ini  README.md
#                    QUICKSTART.md  plan.md
#    - Seed data (REQUIRED for first-run boot):
#        data/cached_universe.json         (~1.2 MB, 10k+ rows)
#        data/cached_crypto_universe.json
#        data/coingecko_catalog_cache.json
#        data/coingecko_coin_list_cache.json
#        data/nasdaq_full_listing.json
#        data/sec_ticker_cik.json
#        app/data/cached_universe.json     (small fallback)
#        app/data/quote_cache.json         (warm quote cache)
#
#  What gets EXCLUDED (kept out on purpose):
#    - Runtime / volatile caches:
#        data/daily_history_cache.json     (45+ MB, regenerates on demand)
#        data/regulatory.db , .db-journal  (rebuilt by Regulatory Monitor)
#        data/user_added_symbols.json      (user-specific)
#    - Dev/IDE noise:
#        __pycache__/  *.pyc  .git/  .venv/  venv/  node_modules/
#        .deps_installed  .emergent/  backend/  memory/  test_reports/
#        dist/  yarn.lock
#
#  CRITICAL: This script is the single source of truth for what ships
#  to end users. Do NOT delete or generalise the explicit `data/...`
#  include list - the app crashes at startup without those seed files.
# ============================================================

set -euo pipefail
cd "$(dirname "$0")/.."

ROOT=$(pwd)
DIST="$ROOT/dist"
ZIP_NAME="market-refinement-dashboard.zip"
ZIP_PATH="$DIST/$ZIP_NAME"

mkdir -p "$DIST"
rm -f "$ZIP_PATH"

# ---- Required seed files (fail loudly if any are missing) -----
REQUIRED_SEEDS=(
  "data/cached_universe.json"
  "data/cached_crypto_universe.json"
  "data/coingecko_catalog_cache.json"
  "data/coingecko_coin_list_cache.json"
  "data/nasdaq_full_listing.json"
  "app/data/cached_universe.json"
)

MISSING=()
for f in "${REQUIRED_SEEDS[@]}"; do
  if [ ! -f "$ROOT/$f" ]; then
    MISSING+=("$f")
  fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "[ERROR] Required seed files missing from working tree:" >&2
  for f in "${MISSING[@]}"; do echo "        - $f" >&2; done
  echo "" >&2
  echo "        These files are required for the app to boot on a" >&2
  echo "        fresh install. Refusing to build a broken zip." >&2
  exit 2
fi

# ---- Build the include list (NUL-separated, fed to zip -@) ----
INCLUDE_LIST=$(mktemp)
trap 'rm -f "$INCLUDE_LIST"' EXIT

# Source trees (find filters out junk via -prune)
for d in app frontend tests scripts docs; do
  if [ -d "$ROOT/$d" ]; then
    find "$d" \
      \( -name '__pycache__' -o -name '*.pyc' -o -name '.pytest_cache' -o -name 'quote_cache' -o -name 'public_url.txt' \) -prune \
      -o -type f -print >> "$INCLUDE_LIST"
  fi
done

# Top-level launcher / config / doc files
for f in start.bat start.sh start_regulatory.bat start_regulatory.sh \
         tunnel_watcher.ps1 \
         verify_startup.py requirements.txt pytest.ini \
         README.md QUICKSTART.md plan.md; do
  [ -f "$ROOT/$f" ] && echo "$f" >> "$INCLUDE_LIST"
done

# Explicit seed-data allowlist (do NOT replace with `data/*` - that would
# sweep in the 45MB daily_history_cache.json and the regulatory SQLite DB).
SEED_FILES=(
  "data/cached_universe.json"
  "data/cached_crypto_universe.json"
  "data/coingecko_catalog_cache.json"
  "data/coingecko_coin_list_cache.json"
  "data/nasdaq_full_listing.json"
  "data/sec_ticker_cik.json"
  "app/data/cached_universe.json"
  "app/data/quote_cache.json"
  "app/data/.gitkeep"
)
for f in "${SEED_FILES[@]}"; do
  [ -f "$ROOT/$f" ] && echo "$f" >> "$INCLUDE_LIST"
done

# Sort + dedupe (defensive - in case find and the seed list overlap)
sort -u "$INCLUDE_LIST" -o "$INCLUDE_LIST"

# ---- Build the zip --------------------------------------------
cd "$ROOT"
zip -q -X "$ZIP_PATH" -@ < "$INCLUDE_LIST"

# ---- Verify the produced archive ------------------------------
echo ""
echo "[ok] Built $ZIP_PATH"
SIZE=$(stat -c '%s' "$ZIP_PATH" 2>/dev/null || stat -f '%z' "$ZIP_PATH")
echo "     size: $SIZE bytes"

echo ""
echo "[verify] critical seed files inside the zip:"
for f in "${REQUIRED_SEEDS[@]}"; do
  if unzip -l "$ZIP_PATH" "$f" >/dev/null 2>&1; then
    sz=$(unzip -l "$ZIP_PATH" "$f" | awk 'NR==4 {print $1}')
    echo "         OK   $f ($sz bytes)"
  else
    echo "         FAIL $f  <-- MISSING (build is broken)"
    exit 3
  fi
done

echo ""
echo "[verify] runtime caches correctly EXCLUDED:"
for f in data/daily_history_cache.json data/daily_history_cache.json.migrated data/regulatory.db data/regulatory.db-journal data/saved_predictions.db data/saved_predictions.db-journal data/saved_predictions.db-wal data/known_bad_symbols.json data/user_added_symbols.json data/user_api_keys.json data/prediction_history.jsonl cloudflared cloudflared.exe app/data/public_url.txt data/public_url.txt; do
  if unzip -l "$ZIP_PATH" "$f" >/dev/null 2>&1; then
    echo "         FAIL $f  <-- should NOT be in the zip"
    exit 4
  fi
done
# Verify the sharded daily-history cache directory is not in the zip either.
# Each shard is 1-14 MB and totals ~80 MB at full warmth; users regenerate
# locally on first run.
if unzip -l "$ZIP_PATH" 'data/daily_history_cache/*' >/dev/null 2>&1; then
  echo "         FAIL data/daily_history_cache/ shards <-- should NOT be in the zip"
  exit 4
fi
echo "         OK   data/daily_history_cache.json + .migrated + cache/ shards (excluded)"

# ---- Verify requirements.txt is clean (no Emergent-sandbox leftovers) ----
# A previous session accidentally ran `pip freeze > requirements.txt` and
# swept in `emergentintegrations` (an internal package not on public PyPI),
# plus a pile of unrelated cloud SDKs (`litellm`, `openai`, `boto3`,
# `motor`, `pymongo`, `google-genai`, `stripe`, ...).  That broke
# `pip install -r requirements.txt` for end users.  Fail loudly here so
# the same mistake can never ship again.
echo ""
echo "[verify] requirements.txt has no Emergent/sandbox-only leftovers:"
FORBIDDEN_PKGS_RE='^(emergentintegrations|litellm|openai|anthropic|stripe|boto3|botocore|s3transfer|s5cmd|motor|pymongo|google-genai|google-generativeai|google-ai-generativelanguage|google-api-python-client|google-api-core|google-auth|google-auth-httplib2|huggingface[_-]hub|tiktoken|tokenizers|hf-xet|passlib|python-jose|PyJWT|bcrypt|cryptography|cffi|curl_cffi|fastuuid|jq|py-spy|black|flake8|isort|mypy)([=<>!~ ]|$)'
BAD=$(unzip -p "$ZIP_PATH" requirements.txt | grep -E "$FORBIDDEN_PKGS_RE" || true)
if [ -n "$BAD" ]; then
  echo "         FAIL requirements.txt contains forbidden Emergent/sandbox packages:"
  echo "$BAD" | sed 's/^/                /'
  echo ""
  echo "  Fix: keep requirements.txt as a hand-curated list of direct deps only."
  echo "  Do NOT run 'pip freeze > requirements.txt' inside the Emergent sandbox."
  exit 5
fi
echo "         OK   no forbidden packages in bundled requirements.txt"

echo ""
echo "[done] zip ready: $ZIP_PATH"
