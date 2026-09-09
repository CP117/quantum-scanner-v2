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

## RAM-first cache operations

The in-process cache foundation is enabled by default. Set
`MEMORY_CACHE_MODE=disabled` for an immediate rollback, `shadow_write` to
populate without serving values, `shadow_compare` to exercise non-serving
refresh coordination, or `enabled` to serve compatible entries.
`MEMORY_CACHE_MAX_MB` defaults to 256 and is a **per-Uvicorn-worker** budget;
the supplied launchers start one worker. Cache diagnostics are available at
`/api/cache/dedupe/memory/status` and intentionally expose only aggregate
counts and approximate memory use. Restarting clears RAM entries; the existing
sharded quote and daily-history stores remain the durable fallback.

### Daily-history RAM residency

Normalized OHLCV frames are resident by normalized symbol, provider, interval,
lookback, adjustment mode, session behavior, and cache namespace. The
canonical frame is immutable by contract: pandas Copy-on-Write is enabled and
cache reads return shallow views, so reads share blocks without allocating a
full DataFrame copy. A consumer that assigns values, columns, dtypes, or
sorts in place receives its own copy; it must not rely on that mutation being
visible to another consumer. With the default five OHLCV columns, actual
footprint is tracked by the memory-store byte budget rather than an assumed
per-symbol size. The initial 90-row, five-column fixture measured 3,732 bytes
with the existing default dtypes; use runtime accounting for production budget
planning because providers, indexes, and column layouts vary. Do not narrow
dtypes or reuse larger windows until a provider-specific compatibility and
precision benchmark proves it safe.

Entries use `time.monotonic()` TTLs. Set `CACHE_TTL_JITTER_PERCENT` (default
`0.10`) to spread expiry, and `MEMORY_CACHE_SINGLEFLIGHT_TIMEOUT_SECONDS`
(default `2.0`) to bound waiters for the same in-process refresh. A timed-out
waiter follows its normal uncached path; the cache lock is never held during
that wait, disk access, provider access, or calculation.

Each domain can be rolled out independently with `CACHE_ENABLE_<DOMAIN>` and
`CACHE_MODE_<DOMAIN>`, for example `CACHE_ENABLE_OPTIONS_CHAINS=1` and
`CACHE_MODE_OPTIONS_CHAINS=shadow_write`. Supported modes are `disabled`,
`shadow_write`, `shadow_compare`, and `enabled`. New derived-score, narrative,
and Bayesian-prior domains default to disabled until their compatibility and
fixture benchmarks are complete. Options-chain and universe-metadata RAM
caches are enabled by default; their TTLs are
independently configurable through `QUOTE_CACHE_TTL_SECONDS`,
`DAILY_HISTORY_CACHE_TTL_SECONDS`, `OPTIONS_CHAIN_CACHE_TTL_SECONDS`, and the
`UNIVERSE_METADATA_TTL_SECONDS` variables. Tier 1 only refreshes an options
chain after `OPTIONS_CHAIN_TIER1_REFRESH_SECONDS` (default 30 seconds).
Tier 1 quote fallback is capped by `TIER1_QUOTE_MAX_AGE_SECONDS` (default
15); a promotion queues a full Tier 1 scoring pass rather than treating an
older last-good quote as fresh. Daily history remains valid under its own
provider/interval contract and is retained across tier changes.

At `MEMORY_CACHE_EMERGENCY_PERCENT` (default 90% of the per-worker budget),
Tier 3 history prefetch is paused. Maintenance checkpoints Tier 3 summaries
then evicts Tier 3 derived artifacts first until
`MEMORY_CACHE_EMERGENCY_RECOVERY_PERCENT` (default 75%). Provider admission
reserves `PROVIDER_TIER3_MIN_REQUESTS_PER_MINUTE` requests so continuous Tier
1/2 work cannot starve Tier 3; `PROVIDER_CIRCUIT_BREAKER_SECONDS` bounds
retries after consecutive provider failures.
Use `POST /api/cache/dedupe/memory/invalidate?domain=options_chains`,
`universe_metadata`, `scanner_presets`, `narratives`, or `bayesian_priors`
for an explicit process-local invalidation; options invalidation retains
provider failure cooldowns.

### Freshness and provenance contract

| Domain | Freshness rule | Stale fallback | Internal provenance |
|---|---|---|---|
| Tier 1/2/3 quotes | Provider/session-appropriate short TTL | Only current-path stale-if-error behavior | provider, source timestamp, generated/cache time |
| Daily history | Latest validated completed bar | Validated completed bar during refresh | provider, bar timestamp, interval, lookback, adjustment mode |
| Options chains | Short provider-aware TTL | Bounded stale-if-error only where already permitted | provider, chain timestamp, expiration/filter selection |
| Scores | Compatible material-input fingerprint | Never serve incomplete or partial scores | component fingerprints, source timestamps, scoring version |
| Forecasts | Model/input generation compatibility | Never present stale output as live | input and model versions, generated timestamp |
| Bayesian priors | Validated source history plus model configuration | Never cache incomplete or unvalidated calibration inputs | model/version, history fingerprint, segment |
| Factor narratives | Complete factor/score inputs plus template configuration | Regenerate incomplete inputs | template/version, input fingerprint |

These fields remain internal during cache migration. Existing result/detail
payloads retain their current `as_of_utc`, `age_seconds`, `freshness_label`,
`stale`, and `data_source` fields; cache keys, raw provider payloads, headers,
and credentials are never exposed through diagnostics.

Bayesian-prior and factor-narrative caches remain opt-in
(`CACHE_ENABLE_BAYESIAN_PRIORS=1` and `CACHE_ENABLE_NARRATIVES=1`). Their
TTLs are `BAYESIAN_PRIOR_TTL_SECONDS` and `NARRATIVE_CACHE_TTL_SECONDS`.
There is no tenant context in these calculation paths; if one is introduced,
it must be isolated in the cache key or the affected artifact must not be
cached.

### Daily-history RAM residency

Daily-history requests now have a provider-aware in-memory identity containing
symbol, provider, interval, period/lookback, adjustment mode, session behavior,
and cache namespace. Cached DataFrames are canonicalized through the existing
deduplication path and returned as deep copies, so a consumer cannot mutate a
resident frame seen by another request. The original symbol-only disk shards
remain a fallback only for legacy default requests; no larger-window reuse is
performed until source-range compatibility is explicitly proven.

Tier 1 and Tier 2 frames receive higher residency priority. Startup warmup is
gated by `MEMORY_CACHE_WARMUP_ENABLED`, delayed per process by up to
`MEMORY_CACHE_WARMUP_JITTER_SECONDS`, and drops Tier 3 prefetch requests when
the shared cache reaches 90% of its configured budget. A synthetic 90-bar,
five-column probe measured 3,732 bytes using default dtypes versus 2,292 bytes
using narrower prices; dtype conversion is deferred pending real-fixture
precision validation.

### Deployment topology and worker budgets

`start.bat` and `start.sh` invoke Uvicorn without `--workers`, so the supported
default is one Python process with one 256 MB cache budget. A module-level
cache, its locks, and any future single-flight work are process-local; they do
not coordinate between Uvicorn or Gunicorn workers.

When deploying multiple workers, set `MEMORY_CACHE_MAX_MB` as a per-worker
limit and budget the host for `worker_count * MEMORY_CACHE_MAX_MB`. The
diagnostic endpoint reports worker count from `WEB_CONCURRENCY`,
`GUNICORN_WORKERS`, or `UVICORN_WORKERS`, plus the estimated all-worker
budget. Until a shared backend is deliberately added, provider snapshots,
warmups, invalidations, and rate limits must be treated as per-process.

### Baseline profiling gate

Before migrating a new cache domain, run the deterministic offline harness:

```bash
python scripts/profile_cache_baseline.py --iterations 50 --output .\profile-cache-baseline.json
```

The JSON report records CPU and wall time, `tracemalloc` allocations, garbage
collection counts, fake-provider calls/failures, top cumulative CPU functions,
and concurrent same-symbol duplicate fetches. The output file is an operational
artifact: do not commit it. Run the same workload before and after each domain
migration; staging measurements must supplement it with live-provider, disk-I/O,
RSS, queue-depth, and lock-wait observations.

The initial fixture run identified cold Python/module initialization and
`score_from_prices()` as the hot path, while the warmed score path is dominated
by `build_algorithm_breakdown()` and `_compute_context_families()`. It also
establishes eight duplicate provider calls for eight concurrent same-symbol
requests, which is the acceptance baseline for a future single-flight refresh.

## 8. Update the source

To re-download the latest bundle from a running server:

```
GET http://localhost:8001/api/download/source.zip
```

or add `?force=1` to force a fresh rebuild.

---

Questions? See `README.md` for architecture details.
