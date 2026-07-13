# Quantum Market Scanner — Local Setup

A one-page guide to get the scanner running on your own machine after
unzipping the source bundle.

## 1. Prerequisites

- Python **3.11+** (3.12 recommended)
- ~2 GB free disk (provider caches grow over time)
- No API keys needed for the default free-provider path

## 2. Install

```bash
cd quantum-market-scanner
python3 -m venv .venv
source .venv/bin/activate           # on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Configure (optional)

```bash
cp .env.example .env
# edit .env if you have Finnhub or CoinGecko Pro keys — everything works without them
```

## 4. Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

The `start.sh` / `start.bat` scripts wrap this if you prefer.

## 5. Open the dashboard

- Main scanner:   http://localhost:8001/frontend/market-refinement-dashboard.html
- Metrics hub:    http://localhost:8001/frontend/metrics-hub.html
- Cross-market squeeze radar: http://localhost:8001/frontend/cross-market-squeeze.html
- Shared analyses gallery:    http://localhost:8001/frontend/shared-analyses.html

## 6. First-run notes

- The scanner needs **~2 minutes** of warmup before the first batch of scored
  rows appears — it prewarms daily-history caches on startup.
- The bundle ships with **baseline `data/*.json` files** (leveraged universe,
  NASDAQ listing, CoinGecko catalog, SEC ticker-CIK map, active-universe
  defaults) so the app can boot and start scanning immediately — no waiting
  for a full CoinGecko refresh cycle.
- The **crypto tab**: click "Crypto market" in the sidebar (or activate any
  crypto universe from the "Scan universes" panel — the tab auto-switches).
  Crypto rows populate within ~30 s of a fresh start.
- All runtime caches live in `./data/daily_history_cache/`, `./data/quote_cache/`,
  etc. Safe to delete — they regenerate.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: emergentintegrations` | Run `pip install -r requirements.txt` inside the venv |
| Crypto list is empty even after 5 min | Ensure you have internet access; CoinGecko free tier can throttle heavily. The scanner will fall back to the hard-seeded top-60 crypto majors so *something* always populates |
| Port 8001 in use | `uvicorn app.main:app --port 8002` and adjust the frontend URL |
| `yfinance` rate limit warnings | Expected during heavy scans — the fallback providers pick up the slack automatically |

## 8. Update the source

To re-download the latest bundle from a running server:

```
GET http://localhost:8001/api/download/source.zip
```

or add `?force=1` to force a fresh rebuild.

---

Questions? See `README.md` for architecture details.
