# Phase 3 — Market-data ingestion boundary

Status: **in progress** — first end-to-end slice (one stock provider, small cohort) implemented; not complete.

Code: `app/ingestion/` (additive; no existing provider or scoring code was changed).

## Contracts (`contracts.py`)
Quote, OHLCVBar, Fundamentals, OptionsChain, ShortInterest, MarketSession. Each carries
`IngestionMeta`: provider, source timestamp (+ timestamp semantics, `ambiguous` by default),
ingestion timestamp, interval, adjustment mode, currency, venue, quality, source revision.
Unknown values are `None`/`UNKNOWN`, never guessed.

## State distinctions
Every value is a `Field` with a `DataState`: `ok`, `zero` (legitimate zero), `unsupported`,
`unavailable` (missing/failed), `stale` (present but too old; not `usable`).

## Semantics (`semantics.py`)
Equity sessions with caller-supplied holidays/hours (no calendar is guessed; DST is the
caller's responsibility), continuous crypto, completed vs forming bars (`finalize_bars`
drops forming bars, orders, de-dupes; later ingestion wins; ambiguous timestamps never
override unambiguous ones), late/out-of-order detection. Corporate actions are carried as
a note on bars and via `AdjustmentMode`; `assert_compatible` raises rather than blending
providers, adjustment modes, intervals or currencies.

## Resilience (`resilience.py`)
Batches capped at 200, retries capped at 5, capped exponential backoff, sliding-window
per-provider rate budget, circuit-style provider health.

## Rights policy (`rights.py`)
Default deny. Public display/redistribution requires an explicit `Entitlement` with a
non-empty `verified_by`. Before exposing any provider's data publicly, verify its license
and exchange entitlements and register them; this layer never bypasses licensing.

## First end-to-end slice
- **Provider/cohort:** `yahoo-chart` (existing provider; raw chart-API JSON). Cohort `AAPL, MSFT, JPM, SPY, KO`
  (`app/ingestion/cohort.py`). Reviewed venue aliases: `NMS→XNAS`, `NYQ→XNYS`, `PCX→ARCX`; unknown/mismatched
  venue labels are rejected, never guessed. Only quotes are adapted (`yahoo_chart_adapter.py`; bars, fundamentals,
  options, short interest, crypto are not).
- **Instrument identity:** quotes carry the canonical `instrument_id` resolved via `provider_instrument_mapping`
  (unmapped/ambiguous symbols are skipped and counted).
- **Timestamps:** only epoch-seconds within a sane range are trusted (`provider_utc`); anything else
  (missing, ms, future, non-numeric) is stored with NULL `source_ts` and `ambiguous` semantics.
- **Storage (`market_state_store.py`):** `market_state_latest` keyed by (instrument, provider, kind) in the catalog
  SQLite file, own `market_state_*` tables/marker (catalog `user_version` untouched; no destructive migration).
  Ordering: older → `rejected_older`; same ts same values → `duplicate`; same ts different values →
  `rejected_conflict`; unknown ts never replaces a known-ts row (`rejected_unknown_ts`); unknown ts over unknown/none
  is applied in ingestion order. Providers are never merged; each is returned separately.
  This extends the catalog DB boundary: SQL for these tables lives only in this module, connection-injected.
- **Failover:** `fetch_with_failover` tries sources in explicit order; the stored/returned provider is the one that
  served the data, and the run summary labels `(failover)`. Failures write nothing; the catalog and last stored quote stay.
- **Command:** `python -m app.ingestion.cli bootstrap|ingest [--db P] [--fixtures DIR]`. Not run at startup, import or
  by any GET route.
- **Read API:** `GET /api/market/catalog` (same filters/cursor as `/api/catalog/instruments`). Read-only; no provider
  calls, no analysis. Instruments without quotes remain with `market_state: null`. Stale is evaluated at read time
  (`max_age_seconds`, default 900) so `stale` is distinct from `unavailable`, `unsupported` and `zero`.

## Rights policy (`rights.py`)
Default deny. Public display/redistribution requires an explicit `Entitlement` with a non-empty `verified_by`.
Configure `MARKET_DATA_DISPLAY_ENTITLEMENTS="provider:kind:verified_by,..."` (e.g. `yahoo-chart:quote:<reviewer>`);
the default is empty, so stored quotes are **withheld** (`market_state_withheld: display_rights_not_verified`) from
`/api/market/catalog`. Verify the provider's license/exchange entitlements before setting it; Yahoo's public endpoint
terms have NOT been verified here. This layer never bypasses licensing.

## Known gaps / next steps
Other data kinds and providers, crypto, market-session-aware staleness (stale uses a flat age), persistence of bars,
automatic scheduling of ingestion, wiring other routes through the rights check, a real exchange calendar, and
migrating analytics to consume normalized records. Phase 3 stays in progress until these are addressed.
