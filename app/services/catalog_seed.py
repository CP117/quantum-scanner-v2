"""Controlled seed/import of existing universe lists into the catalog.

Operator command; never invoked by HTTP requests or app startup::

    python -m app.services.catalog_seed --market stocks            # dry run
    python -m app.services.catalog_seed --market stocks --apply
    python -m app.services.catalog_seed --universe-file rows.json --apply --db /path/catalog.db

Rules: venue identity is taken only from the row's ``exchange`` field and is
never invented; rows without a usable venue are skipped and counted.  Crypto
rows in the legacy universe carry only the placeholder exchange ``CRYPTO``
and ``BTC-USD``-style symbols (no real venue/pair identity), so they are
skipped until a provider supplies venue data.  Seeding is a *partial*
listing by default (no absence counting) because the legacy active-universe
list can shrink for operational reasons; pass ``--complete`` to count
absences.  ``instrument_type`` is not inferred (legacy rows carry none).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Iterable, Optional

from app.services import catalog_service as cs

PROVIDER = 'legacy_universe'
_PLACEHOLDER_VENUES = {'CRYPTO', 'UNKNOWN', 'N/A', ''}


def universe_rows_to_records(rows: Iterable[dict], market: str = 'stocks') -> tuple[list[dict], dict]:
    """Pure adapter: legacy universe rows -> catalog records + skip counts."""
    records: list[dict] = []
    skipped: Counter = Counter()
    for r in rows:
        sym = str(r.get('symbol') or '').strip()
        if not sym:
            skipped['missing_symbol'] += 1
            continue
        if market != 'stocks':
            skipped['crypto_venue_unknown'] += 1
            continue
        venue = str(r.get('exchange') or '').strip()
        if venue.upper() in _PLACEHOLDER_VENUES:
            skipped['missing_venue'] += 1
            continue
        records.append({'asset_class': 'equity', 'venue': venue, 'symbol': sym, 'name': r.get('name'),
                        'provider_symbol': sym})
    return records, dict(skipped)


def seed(rows: Iterable[dict], market: str = 'stocks', apply: bool = False, complete: bool = False,
         universe_key: Optional[str] = None, db: Optional[str] = None) -> dict:
    records, skipped = universe_rows_to_records(rows, market)
    report = {'market': market, 'candidate_records': len(records), 'skipped': skipped, 'applied': False}
    if not apply:
        return report
    conn = cs.connect(db)
    try:
        result = cs.sync_listing(conn, PROVIDER, universe_key or f'legacy_{market}_active', records,
                                 complete=complete)
    finally:
        conn.close()
    report.update(applied=True, run=result)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--market', choices=('stocks', 'crypto'), default='stocks')
    ap.add_argument('--universe-file', help='JSON list of {symbol,name,exchange} rows (default: universe_service)')
    ap.add_argument('--apply', action='store_true', help='write to the catalog (default is a dry run)')
    ap.add_argument('--complete', action='store_true', help='treat the list as complete (enables absence counting)')
    ap.add_argument('--universe-key')
    ap.add_argument('--db', help='catalog DB path (default: settings.catalog_db_path)')
    args = ap.parse_args(argv)
    if args.universe_file:
        with open(args.universe_file, encoding='utf-8') as fh:
            rows = json.load(fh)
    else:
        from app.services import universe_service
        rows = universe_service.load_universe(args.market)
    print(json.dumps(seed(rows, args.market, args.apply, args.complete, args.universe_key, args.db),
                     indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
