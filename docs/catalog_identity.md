# Catalog instrument identity and rename continuity

Status: Phase 2, pending validation.

## Identity

`instrument_id = "ins_" + sha256(asset_class | venue | native_symbol)[:20]`.

- `asset_class` ∈ `equity`, `crypto_pair`, `crypto_asset`. The finer
  `instrument_type` (stock, etf, ...) is a mutable attribute and deliberately
  **not** part of the identity, so reclassification never changes an ID.
- `venue` is mandatory and never guessed: upper-cased, whitespace → `_`. No
  MIC/alias mapping exists yet, so providers that name one venue differently
  will produce different instruments until an explicit alias table is added.
- Equity symbols: `-`, `/`, whitespace → `.` so `BRK-B`, `BRK/B`, `BRK.B`
  resolve together across providers; share classes remain distinct symbols.
- Crypto pairs: `BASE-QUOTE` with an explicit quote; the same pair on two
  exchanges differs by venue. An aggregated asset has no quote and is a
  different identity from every pair. Base symbols may not contain separators.
- Provider symbols are stored in `provider_instrument_mapping`; a provider
  symbol that maps to more than one instrument is ambiguous and
  `resolve_provider_symbol` returns all candidates.

## Symbol renames / identity continuity

Because the ID is derived from the natural key, a ticker change produces a
*new* instrument. Continuity strategy (usable now, detection deferred):

1. IDs are never reused or deleted. Downstream subsystems (quotes, analysis,
   predictions, watchlists) must store `instrument_id`, never a ticker.
2. An operator/import step records the relationship explicitly with
   `link_succession(old_id, new_id, reason, effective_at)`; the old instrument
   becomes `superseded` (explicit-only; sync never resurrects it) and its
   history is retained.
3. Consumers that follow an ID call `resolve_canonical_id(id)` to reach the
   current instrument; chains are followed and cycles are rejected. Historical
   records keep the ID they were issued under (issued predictions are never
   rewritten).
4. Deferred: automatic detection (e.g. same provider ID/FIGI-style identifier
   under a new symbol), merger/split handling, and migrating consumers' history
   to the successor.
