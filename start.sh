#!/usr/bin/env bash
# ============================================================
#  Market Refinement Dashboard - macOS / Linux startup
# ============================================================
#  Mirrors start.bat: creates .venv on first run, installs
#  dependencies, downloads cloudflared for off-network access,
#  launches the FastAPI backend on port 8001, opens the
#  dashboard in the default browser, and prints local + LAN +
#  PUBLIC URLs so anyone (any device, any network) can connect.
#
#  Usage:  ./start.sh   (run from this directory)
#  Stop :  press Ctrl+C   (cleans up cloudflared automatically)
# ============================================================
set -uo pipefail
cd "$(dirname "$0")"

echo ""
echo "============================================================"
echo " Market Refinement Dashboard - starting up"
echo "============================================================"
echo ""

# --- 1) locate Python 3.11+ ---------------------------------
find_python() {
  for cand in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      vers=$("$cand" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)
      if [ "${vers:-0}" -ge 311 ]; then
        echo "$cand"
        return 0
      fi
    fi
  done
  return 1
}

PYEXE=$(find_python || true)
if [ -z "${PYEXE:-}" ]; then
  echo "[ERROR] Python 3.11 or newer is required but was not found."
  echo "        macOS:  brew install python@3.12"
  echo "        Linux:  use your package manager (apt, dnf, pacman, ...)"
  exit 1
fi
echo "[ok] Using Python interpreter: $PYEXE ($($PYEXE --version 2>&1))"

# --- 2) create venv if missing ------------------------------
if [ ! -x ".venv/bin/python" ]; then
  echo "[info] Creating virtual environment in .venv ..."
  "$PYEXE" -m venv .venv
fi
VENV_PY="$(pwd)/.venv/bin/python"
echo "[ok] Virtual environment ready."

# --- 3) install dependencies on first run -------------------
if [ ! -f ".deps_installed" ]; then
  echo "[info] Installing Python dependencies (first run, takes a few minutes) ..."
  "$VENV_PY" -m pip install --upgrade pip
  "$VENV_PY" -m pip install -r requirements.txt
  touch .deps_installed
  echo "[ok] Dependencies installed."
else
  echo "[ok] Dependencies already installed. To force re-install: rm .deps_installed"
fi

# --- 4) download cloudflared once ---------------------------
#   cloudflared = Cloudflare's tunnel binary.  Free, no signup, gives us
#   an anonymous https://xxx-yyy-zzz.trycloudflare.com URL that proxies
#   straight to the local backend - lets devices on OTHER networks
#   connect without router port-forwarding or DDNS.
ensure_cloudflared() {
  if [ -x "./cloudflared" ]; then return 0; fi

  local os arch url
  os=$(uname -s | tr '[:upper:]' '[:lower:]')
  arch=$(uname -m)
  case "$os/$arch" in
    linux/x86_64|linux/amd64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64";;
    linux/aarch64|linux/arm64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64";;
    darwin/x86_64|darwin/amd64)
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz";;
    darwin/arm64|darwin/aarch64)
      # No native arm64 binary published; the amd64 binary runs under Rosetta.
      url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz";;
    *)
      echo "[warn] No cloudflared binary published for $os/$arch."
      echo "       The app will still run locally / on the LAN. Skipping."
      return 1;;
  esac

  echo "[info] Downloading cloudflared for $os/$arch (one-time, ~30 MB)..."
  if command -v curl >/dev/null 2>&1; then
    if [[ "$url" == *.tgz ]]; then
      curl -L --fail --silent --show-error -o /tmp/cloudflared.tgz "$url" || return 1
      tar -xzf /tmp/cloudflared.tgz -C .
      rm -f /tmp/cloudflared.tgz
    else
      curl -L --fail --silent --show-error -o ./cloudflared "$url" || return 1
    fi
  elif command -v wget >/dev/null 2>&1; then
    if [[ "$url" == *.tgz ]]; then
      wget -q -O /tmp/cloudflared.tgz "$url" || return 1
      tar -xzf /tmp/cloudflared.tgz -C .
      rm -f /tmp/cloudflared.tgz
    else
      wget -q -O ./cloudflared "$url" || return 1
    fi
  else
    echo "[warn] Neither curl nor wget available - cannot fetch cloudflared."
    return 1
  fi

  chmod +x ./cloudflared
  echo "[ok] cloudflared downloaded."
  return 0
}

CFD_AVAILABLE=0
if ensure_cloudflared; then
  CFD_AVAILABLE=1
fi

# --- 5) pick a free port ------------------------------------
PORT=8001
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || \
   (command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$PORT$"); then
  echo "[warn] Port $PORT is already in use. Falling back to 8011."
  PORT=8011
fi

# --- 6) start the Cloudflare Quick Tunnel in the background -
CFD_PID=""
CFD_LOG="/tmp/mrd_cloudflared.$$.log"
PUBLIC_URL=""

# Phase 26.5: persist the captured public URL + LAN URLs to a known file
# so the frontend can show them in the dashboard header. The launcher
# wipes this on every start so a stale URL from a previous session can't
# mislead the user.
PUBLIC_URL_FILE="app/data/public_url.txt"
mkdir -p "$(dirname "$PUBLIC_URL_FILE")" 2>/dev/null
: > "$PUBLIC_URL_FILE"

# Helper: rewrite app/data/public_url.txt with the current best URLs.
# Called once after the initial detection and again by the watcher below
# if cloudflared takes longer than the 12s pre-launch wait to negotiate.
write_public_url_file() {
  local public_url="$1"
  {
    if [ -n "$public_url" ]; then
      echo "$public_url"
    fi
    for ip in $(discover_lan_ips 2>/dev/null); do
      case "$ip" in
        127.*|169.254.*) ;;
        *) echo "http://$ip:$PORT" ;;
      esac
    done
    echo "http://localhost:$PORT"
  } > "$PUBLIC_URL_FILE"
}

# Pre-declare so write_public_url_file can call it before it's defined.
discover_lan_ips() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
  elif command -v ifconfig >/dev/null 2>&1; then
    ifconfig 2>/dev/null | awk '/inet / && $2 !~ /^127\./ && $2 !~ /^169\.254\./ {print $2}'
  fi
}

if [ "$CFD_AVAILABLE" = "1" ]; then
  : > "$CFD_LOG"
  ./cloudflared --no-autoupdate tunnel --url "http://localhost:$PORT" \
      >"$CFD_LOG" 2>&1 &
  CFD_PID=$!

  # Wait up to 12 seconds for the trycloudflare.com URL to appear.
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    PUBLIC_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CFD_LOG" 2>/dev/null | head -n 1)
    if [ -n "$PUBLIC_URL" ]; then break; fi
    sleep 1
  done

  # Write whatever we have (URL may be empty if cloudflared is slow).
  write_public_url_file "$PUBLIC_URL"

  # Phase 26.5: if cloudflared was slow, keep watching the log in the
  # background and rewrite public_url.txt the moment the URL appears.
  # Bounded loop - bails after 3 minutes regardless.
  if [ -z "$PUBLIC_URL" ]; then
    (
      for _ in $(seq 1 180); do
        sleep 1
        late_url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CFD_LOG" 2>/dev/null | head -n 1)
        if [ -n "$late_url" ]; then
          write_public_url_file "$late_url"
          break
        fi
      done
    ) &
  fi
else
  # No cloudflared - still publish the LAN URLs so the frontend banner has
  # something useful to show.
  write_public_url_file ""
fi

cleanup() {
  if [ -n "${CFD_PID:-}" ] && kill -0 "$CFD_PID" 2>/dev/null; then
    echo ""
    echo "[info] Shutting down cloudflared tunnel..."
    kill "$CFD_PID" 2>/dev/null || true
    wait "$CFD_PID" 2>/dev/null || true
  fi
  rm -f "$CFD_LOG"
  # Phase 26.5: wipe the captured public-URL file so a stale URL from this
  # session doesn't mislead the user on next launch if something crashes
  # before the launcher gets a chance to rewrite it.
  : > "$PUBLIC_URL_FILE" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# --- 7) open the dashboard once the server is up ------------
open_browser() {
  sleep 5
  url="http://localhost:$PORT/ui"
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then open "$url" >/dev/null 2>&1 || true
  fi
}
open_browser &

# --- 7b) Discover LAN IPv4 address(es) ----------------------
# (discover_lan_ips defined above in section 6 so write_public_url_file can use it)

echo ""
echo "============================================================"
echo " Backend launching on:"
echo "   http://localhost:$PORT/ui          (this machine only)"
for ip in $(discover_lan_ips); do
  case "$ip" in
    127.*|169.254.*) ;;
    *) echo "   http://$ip:$PORT/ui    (any device on this LAN)";;
  esac
done
echo ""
if [ -n "$PUBLIC_URL" ]; then
  echo " PUBLIC URL (works from ANY network - share with anyone):"
  echo "   $PUBLIC_URL/ui"
  echo ""
  echo "   * regenerated every time you start the app; only valid"
  echo "     while this window stays open."
  echo "   * anyone with the URL can reach the dashboard, so don't"
  echo "     post it publicly if you want to keep it private."
elif [ "$CFD_AVAILABLE" = "1" ]; then
  echo " PUBLIC URL: still negotiating with Cloudflare (give it a"
  echo "   few more seconds, then check $CFD_LOG)."
else
  echo " PUBLIC URL: not available (cloudflared could not be"
  echo "   downloaded for this platform). LAN access still works."
fi
echo ""
echo " Phones / tablets on the same WiFi can use any LAN URL above."
echo " Press Ctrl+C to stop the server (and the tunnel)."
echo "============================================================"
echo ""

# --- 8) launch FastAPI via uvicorn --------------------------
# Suppress yfinance's internal Pandas FutureWarning / DeprecationWarning floods.
export PYTHONWARNINGS="ignore::FutureWarning,ignore::DeprecationWarning"
"$VENV_PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
