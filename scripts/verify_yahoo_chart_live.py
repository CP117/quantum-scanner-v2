"""OPERATIONAL (non-deterministic) check of the yahoo-chart field/venue mapping against the live
provider. NOT part of the test suite; never run automatically.

    python scripts/verify_yahoo_chart_live.py [SYMBOL ...]

Exit 0 = every symbol parsed and its venue label resolves to the cohort's reviewed alias and
catalog venue; exit 2 = provider unreachable (validation stays PENDING); exit 1 = mismatch.
Check the provider's terms before running it regularly; it makes one request per symbol.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def main() -> int:
    from app.ingestion import cohort
    from app.ingestion.semantics import parse_epoch_seconds  # noqa: F401
    from app.ingestion.yahoo_chart_adapter import adapt_quote, fetch_raw
    import time
    expected = {r['provider_symbol']: r['venue'] for r in cohort.COHORT}
    syms = sys.argv[1:] or list(expected)
    bad = unreachable = 0
    for s in syms:
        try:
            payload = fetch_raw(s)
        except Exception as exc:  # noqa: BLE001
            print(f'{s}: UNREACHABLE {exc}')
            unreachable += 1
            continue
        try:
            q, label = adapt_quote(payload, now_ts=time.time())
        except Exception as exc:  # noqa: BLE001
            print(f'{s}: PARSE-FAIL {exc}')
            bad += 1
            continue
        venue = cohort.VENUE_ALIASES.get(label.upper())
        ok = venue == expected.get(s) and q.price.usable and q.meta.currency == 'USD'
        print(f'{s}: label={label} venue={venue} expected={expected.get(s)} price={q.price.state.value} '
              f'ts={q.meta.source_ts} sem={q.meta.timestamp_semantics.value} cur={q.meta.currency} '
              f'vol={q.volume.state.value} -> {"OK" if ok else "MISMATCH"}')
        bad += 0 if ok else 1
    if bad:
        return 1
    return 2 if unreachable else 0


if __name__ == '__main__':
    sys.exit(main())
