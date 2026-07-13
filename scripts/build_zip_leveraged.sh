#!/usr/bin/env bash
# ============================================================
#  Market Refinement Dashboard — LEVERAGED-ONLY variant packager
# ============================================================
#  Produces dist/market-refinement-dashboard-leveraged.zip.
#
#  What's different from the main build:
#    1. Injects `app/data/variant.json` declaring
#         {"universe_mode": "leveraged", "disable_crypto": true}
#       which makes `universe_service.is_leveraged_variant()` flip
#       the stocks universe over to `data/leveraged_universe.json`
#       (~270 curated leveraged/inverse ETF tickers) and short-
#       circuits the crypto market entirely.
#    2. INCLUDES `data/leveraged_universe.json` (required seed).
#    3. The main app's `data/cached_universe.json` + crypto seeds
#       are still bundled so the user can switch back to the full
#       universe simply by deleting `app/data/variant.json` after
#       extracting — they're just unused at startup otherwise.
#
#  CRITICAL: this script must NOT pollute the working tree.  It
#  creates `app/data/variant.json` only inside the zip via a
#  temp-dir staging step, never on disk in the repo, so the main
#  `build_zip.sh` run that follows always produces a clean
#  full-universe build.
# ============================================================

set -euo pipefail
cd "$(dirname "$0")/.."

ROOT=$(pwd)
DIST="$ROOT/dist"
ZIP_NAME="market-refinement-dashboard-leveraged.zip"
ZIP_PATH="$DIST/$ZIP_NAME"

mkdir -p "$DIST"
rm -f "$ZIP_PATH"

# ---- Required seed files (fail loudly if any are missing) -----
REQUIRED_SEEDS=(
  "data/leveraged_universe.json"
  "data/cached_universe.json"
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
  echo "        These files are required for the leveraged variant to" >&2
  echo "        boot. Refusing to build a broken zip." >&2
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

# Explicit seed-data allowlist.  We KEEP the full universe seeds so a
# user who wants to flip back to the full build can just remove
# `app/data/variant.json` after extracting — no re-download needed.
SEED_FILES=(
  "data/leveraged_universe.json"
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

# Sort + dedupe (defensive)
sort -u "$INCLUDE_LIST" -o "$INCLUDE_LIST"

# ---- Stage the variant-marker file in a temp dir ---------------
# We want `app/data/variant.json` to appear INSIDE the zip but NOT
# on disk in the working tree (so the running dev server stays in
# full-universe mode).  Strategy: stage a temp dir, build the
# variant marker there, then call `zip -j` to inject it under the
# desired path.
VARIANT_STAGE=$(mktemp -d)
trap 'rm -f "$INCLUDE_LIST"; rm -rf "$VARIANT_STAGE"' EXIT
mkdir -p "$VARIANT_STAGE/app/data"
cat > "$VARIANT_STAGE/app/data/variant.json" <<'JSON'
{
  "universe_mode": "leveraged",
  "disable_crypto": false,
  "build": "market-refinement-dashboard-leveraged",
  "notes": "Default-active universe = Leveraged/Inverse ETFs only (~270+ symbols). All other exchange shards and the two crypto sets (Core / Extended) are available but DEFAULT OFF — activate them from the 'Scan universes' sidebar panel. Delete this file and restart to default every group ON (full ~12k universe)."
}
JSON

# ---- Build the zip --------------------------------------------
cd "$ROOT"
zip -q -X "$ZIP_PATH" -@ < "$INCLUDE_LIST"
# Add the variant marker WITH its directory structure preserved.
# `zip -X -r` from inside the stage dir places the file at the
# correct relative path inside the archive.
(cd "$VARIANT_STAGE" && zip -q -X -r "$ZIP_PATH" "app/data/variant.json")

# ---- Verify the produced archive ------------------------------
echo ""
echo "[ok] Built $ZIP_PATH"
SIZE=$(stat -c '%s' "$ZIP_PATH" 2>/dev/null || stat -f '%z' "$ZIP_PATH")
echo "     size: $SIZE bytes"

echo ""
echo "[verify] critical seed files inside the zip:"
for f in "${REQUIRED_SEEDS[@]}" "app/data/variant.json"; do
  if unzip -l "$ZIP_PATH" "$f" >/dev/null 2>&1; then
    sz=$(unzip -l "$ZIP_PATH" "$f" | awk 'NR==4 {print $1}')
    echo "         OK   $f ($sz bytes)"
  else
    echo "         FAIL $f  <-- MISSING (build is broken)"
    exit 3
  fi
done

echo ""
echo "[verify] variant marker contents:"
unzip -p "$ZIP_PATH" app/data/variant.json | sed 's/^/         /'

echo ""
echo "[verify] runtime caches correctly EXCLUDED:"
for f in data/daily_history_cache.json data/daily_history_cache.json.migrated data/regulatory.db data/regulatory.db-journal data/saved_predictions.db data/saved_predictions.db-journal data/saved_predictions.db-wal data/known_bad_symbols.json data/user_added_symbols.json data/user_api_keys.json data/prediction_history.jsonl cloudflared cloudflared.exe app/data/public_url.txt data/public_url.txt; do
  if unzip -l "$ZIP_PATH" "$f" >/dev/null 2>&1; then
    echo "         FAIL $f  <-- should NOT be in the zip"
    exit 4
  fi
done
if unzip -l "$ZIP_PATH" 'data/daily_history_cache/*' >/dev/null 2>&1; then
  echo "         FAIL data/daily_history_cache/ shards <-- should NOT be in the zip"
  exit 4
fi
echo "         OK   runtime caches excluded"

# ---- Verify requirements.txt is clean (no Emergent-sandbox leftovers) ----
echo ""
echo "[verify] requirements.txt has no Emergent/sandbox-only leftovers:"
FORBIDDEN_PKGS_RE='^(emergentintegrations|litellm|openai|anthropic|stripe|boto3|botocore|s3transfer|s5cmd|motor|pymongo|google-genai|google-generativeai|google-ai-generativelanguage|google-api-python-client|google-api-core|google-auth|google-auth-httplib2|huggingface[_-]hub|tiktoken|tokenizers|hf-xet|passlib|python-jose|PyJWT|bcrypt|cryptography|cffi|curl_cffi|fastuuid|jq|py-spy|black|flake8|isort|mypy)([=<>!~ ]|$)'
BAD=$(unzip -p "$ZIP_PATH" requirements.txt | grep -E "$FORBIDDEN_PKGS_RE" || true)
if [ -n "$BAD" ]; then
  echo "         FAIL requirements.txt contains forbidden Emergent/sandbox packages:"
  echo "$BAD" | sed 's/^/                /'
  exit 5
fi
echo "         OK   no forbidden packages in bundled requirements.txt"

# ---- Verify the working tree was NOT polluted -----------------
# variant.json must ONLY exist inside the zip, never on disk.
if [ -f "$ROOT/app/data/variant.json" ]; then
  echo ""
  echo "[FAIL] $ROOT/app/data/variant.json was created on disk!" >&2
  echo "       The leveraged build script must NOT pollute the" >&2
  echo "       working tree.  Remove it before running again." >&2
  exit 6
fi
echo "         OK   working tree clean (app/data/variant.json NOT on disk)"

echo ""
echo "[done] leveraged zip ready: $ZIP_PATH"
