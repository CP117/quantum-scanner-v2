# Migration parity matrix

| Behavior | Current source of truth | Existing evidence | Baseline/parity action | Status |
|---|---|---|---|---|
| Synthetic stock and crypto market inputs | New sanitized fixture files in `backend/tests/fixtures/parity/` | This increment's deterministic fixture tests | Load fixed OHLCV and preserve market identity; never fetch network data | Scaffolding added |
| Core score components and missing inputs | `scoring_service.py` | Existing focused service tests are partial | Capture component payload, weights, confidence/degraded path, and direction | Not captured |
| Extended-family status and composite | `extended_factors.py`, `scoring_service.py` | Some factor/cache tests | Capture each family, not-applicable/unavailable states, blended and final score | Not captured |
| Presets, filters, and sort | `scanner_presets.py`, `result_store.py`, `/stocks/results` | API integration tests, some live-data-dependent | Fixed rows for each relevant mode; assert stable ordering/null behavior | Not captured |
| Snapshot sorting and ETag response | `routes/snapshot.py`, `snapshot_store.py` | Existing API coverage is partial | Verify `score`/PVI allowed values and fallback behavior | Not captured |
| Provider fallback/failure | Provider services and scoring caller | Crypto wiring tests exercise live endpoints | Deterministic provider stubs/failure injection; do not call providers | Not captured |
| Options and short-pressure applicability | Options and short-pressure services | Crypto wiring assertions and family output checks | Compare stock, crypto, absent data, and not-applicable paths | Partial; exact baselines absent |
| Prediction outputs/horizons | Price, advanced, Future Mode services | Signal-focused unit tests; no uniform output baseline | Capture each actual model/horizon separately and document tolerance | Not captured |
| Performance/resource behavior | Scanner and prediction paths | `scripts/profile_cache_baseline.py` exists | Run controlled representative workloads and record environment and measurements | Not measured here |
| Durable catalog, queue, projection, and frontend scale | Not yet implemented as target architecture | No acceptance evidence | Add tests only when those components exist | Future phase |

The test helper added in this increment writes baselines only when capture is
explicitly enabled, only to the test baseline directory (or a caller-provided
test directory), and refuses to overwrite an existing file. This matrix does
not claim output parity or performance improvements.
