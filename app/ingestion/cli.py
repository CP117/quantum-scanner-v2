"""Explicit ingestion command (never run at startup or by HTTP requests)::

    python -m app.ingestion.cli bootstrap --db data/catalog.db
    python -m app.ingestion.cli ingest --db data/catalog.db            # live yahoo-chart
    python -m app.ingestion.cli ingest --fixtures backend/tests/fixtures/ingestion  # offline

``bootstrap`` loads the reviewed cohort/aliases into the catalog (idempotent). ``ingest``
writes only market_state_* tables and never alters catalog rows.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from app.services import catalog_service as cs

from . import cohort
from .service import Source, run_ingestion
from .yahoo_chart_adapter import PROVIDER, fetch_raw


def _fixture_fetcher(directory: str):
    def fetch(sym: str) -> dict:
        path = Path(directory) / f'yahoo_chart_{sym}.json'
        if not path.is_file():
            raise RuntimeError(f'no fixture for {sym}')
        return json.loads(path.read_text(encoding='utf-8'))
    return fetch


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('command', choices=('bootstrap', 'ingest'))
    ap.add_argument('--db')
    ap.add_argument('--symbols', nargs='*', help='provider symbols (default: cohort)')
    ap.add_argument('--fixtures', help='read raw payloads from this directory instead of the live provider')
    args = ap.parse_args(argv)
    conn = cs.connect(args.db)
    try:
        if args.command == 'bootstrap':
            out = cohort.bootstrap(conn)
        else:
            fetch = _fixture_fetcher(args.fixtures) if args.fixtures else fetch_raw
            symbols = args.symbols or [r['provider_symbol'] for r in cohort.COHORT]
            out = run_ingestion(conn, [Source(PROVIDER, fetch)], symbols)
        print(json.dumps(out, indent=2, default=str))
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
