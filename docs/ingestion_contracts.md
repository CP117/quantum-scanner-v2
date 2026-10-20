# Phase 3 — Market-data ingestion boundary

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

## Known gaps / next steps
Existing provider modules are not yet wrapped by adapters producing these contracts;
analytics still consume raw payloads. Next: adapters per provider, persistence of ingestion
metadata, wiring the rights check into public routes, a real exchange calendar.
