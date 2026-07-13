"""Wipe runtime provider caches (daily history, options, quotes, reaction
maps, dist artifacts) while preserving the *baseline* seed data files
that ship with the scanner.

Use when:
  * providers get funky and cached data feels stale
  * you want to force a fresh CoinGecko / yfinance / SEC re-fetch
  * you're preparing a clean handoff bundle

Safe to run any time — the scanner regenerates every cache dir on the
next scan sweep.  Baseline JSONs (leveraged_universe.json,
cached_universe.json, coingecko_catalog_cache.json, coin_list_cache,
sec_ticker_cik, nasdaq_full_listing, active_universes[.baseline].json)
are **NOT** touched.

Usage
-----
    python scripts/reset_caches.py               # dry-run: show what would go
    python scripts/reset_caches.py --confirm     # actually delete
    python scripts/reset_caches.py --confirm --data-dir /path/to/data
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

# Cache SUBDIRECTORIES under data/.  Everything inside these is
# regenerable — the scanner rebuilds them on the next sweep.
_CACHE_DIRS = [
    'daily_history_cache',
    'quote_cache',
    'options',
    'reaction_maps',
    'reaction_clustering',
    'options_chain',
    'cache_quarantine',
    'active_scan_pool',
    'universes_extra',
    'top10_priority',
    'crypto_provider',
    'symbol_blacklist',
    'result_store',
    'dist',
]

# Individual FILES under data/ that are runtime caches (safe to nuke).
# Baseline seed files (see _PRESERVED below) are never touched.
_CACHE_FILES = [
    'wedge_watchdog.json',
    'share_events.json',
    'saved_predictions.db',
    'saved_predictions.db-wal',
    'saved_predictions.db-shm',
    'regulatory.db',
    'variant.json',
    'public_url.txt',
    'quote_cache.json.migrated',
]

# Explicit allow-list of files the reset MUST preserve.  Used only for
# the audit printout — nothing in this list is ever deleted.
_PRESERVED = [
    'leveraged_universe.json',
    'cached_universe.json',
    'nasdaq_full_listing.json',
    'coingecko_catalog_cache.json',
    'coingecko_coin_list_cache.json',
    'sec_ticker_cik.json',
    'known_bad_symbols.json',
    'active_universes.json',
    'active_universes.baseline.json',
    'user_added_symbols.json',
]


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob('*'):
            if f.is_file():
                total += f.stat().st_size
    except (OSError, PermissionError):
        pass
    return total


def _human(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.1f}{unit}'
        n /= 1024
    return f'{n:.1f}TB'


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    ap.add_argument('--confirm', action='store_true',
                    help='actually delete (otherwise dry-run and print only)')
    ap.add_argument('--data-dir', default='data',
                    help='path to the data directory (default: ./data)')
    args = ap.parse_args()

    data = Path(args.data_dir).resolve()
    if not data.exists():
        print(f'ERROR: {data} does not exist.')
        return 1

    print(f'Data dir: {data}')
    print(f'Mode:     {"DELETE" if args.confirm else "DRY-RUN"}')
    print()

    print('== Preserved (baseline seeds) ==')
    for name in _PRESERVED:
        p = data / name
        if p.exists():
            print(f'  KEEP  {name:<40s} {_human(p.stat().st_size):>10s}')
    print()

    total_freed = 0
    print('== Runtime cache directories ==')
    for d in _CACHE_DIRS:
        p = data / d
        if not p.exists():
            continue
        sz = _dir_size(p)
        total_freed += sz
        print(f'  {"NUKE" if args.confirm else "WOULD"}  {d + "/":<40s} {_human(sz):>10s}')
        if args.confirm:
            try:
                shutil.rmtree(p)
            except (OSError, PermissionError) as exc:
                print(f'         (failed: {exc})')

    print()
    print('== Runtime cache files ==')
    for f in _CACHE_FILES:
        p = data / f
        if not p.exists():
            continue
        sz = p.stat().st_size
        total_freed += sz
        print(f'  {"NUKE" if args.confirm else "WOULD"}  {f:<40s} {_human(sz):>10s}')
        if args.confirm:
            try:
                p.unlink()
            except (OSError, PermissionError) as exc:
                print(f'         (failed: {exc})')

    print()
    print(f'Total {"freed" if args.confirm else "would free"}: {_human(total_freed)}')
    if not args.confirm:
        print()
        print('Add --confirm to actually delete.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
