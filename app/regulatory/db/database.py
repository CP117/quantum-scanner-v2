import aiosqlite
from pathlib import Path

# Live alongside the rest of the scanner's local data so the package stays
# self-contained and the SQLite file is included in any data backups.
DB_PATH = Path(__file__).resolve().parents[3] / 'data' / 'regulatory.db'
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS watchlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cik TEXT NOT NULL,
  recipient TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tracked_companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cik TEXT NOT NULL UNIQUE,
  issuer_name TEXT,
  issuer_ticker TEXT,
  source TEXT DEFAULT 'insider_discovery',
  last_seen_filing_date TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS discovery_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  cik TEXT,
  issuer_name TEXT,
  form_type TEXT,
  filing_date TEXT,
  filing_url TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS filings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  accession_number TEXT,
  filing_date TEXT,
  form TEXT,
  issuer_cik TEXT,
  issuer_ticker TEXT,
  issuer_name TEXT,
  reporting_owner_name TEXT,
  reporting_owner_cik TEXT,
  is_director INTEGER,
  is_officer INTEGER,
  is_ten_percent_owner INTEGER,
  is_other INTEGER,
  officer_title TEXT,
  transaction_code TEXT,
  transaction_type TEXT,
  security_type TEXT,
  shares REAL,
  price_per_share REAL,
  shares_owned_following REAL,
  ownership_nature TEXT,
  percent_owned REAL,
  source_url TEXT,
  raw_excerpt TEXT,
  unique_key TEXT UNIQUE,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS awards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  generated_internal_id TEXT UNIQUE,
  award_id TEXT,
  recipient_name TEXT,
  recipient_uei TEXT,
  awarding_agency TEXT,
  awarding_subagency TEXT,
  action_date TEXT,
  amount REAL,
  description TEXT,
  naics_code TEXT,
  naics_description TEXT,
  psc_code TEXT,
  psc_description TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  alert_type TEXT NOT NULL,
  alert_key TEXT UNIQUE,
  title TEXT NOT NULL,
  body TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
'''

DEFAULT_SETTINGS = {
    'scheduler_interval_seconds': '1800',
    'default_scan_limit': '10',
    'large_award_threshold': '1000000',
    'ownership_threshold_percent': '5',
    # Per integration spec: scheduler + auto-discovery default OFF so a cold
    # download doesn't immediately burn SEC rate-limit budget on first launch.
    # The user toggles them on from the regulatory dashboard's Settings panel.
    'enable_scheduler': '0',
    'enable_entity_linking': '1',
    'enable_auto_discovery': '0',
    'auto_discovery_interval_seconds': '3600',
    # Universe auto-scan — default ON. Walks the scanner's universe every
    # `universe_autoscan_interval_seconds` (default 4h) and scans every ticker
    # we can resolve to a SEC CIK. This is what drives the auto-populating
    # insider + contract-award result list shown on the regulatory page.
    'enable_universe_autoscan': '1',
    'universe_autoscan_interval_seconds': '14400',          # 4 hours
    'universe_autoscan_request_gap_ms': '120',              # ~8 req/sec to SEC
    'universe_autoscan_limit_per_symbol': '3',              # filings per scan
    'universe_autoscan_max_tickers': '0',                   # 0 = whole map
    # Scoring-feedback knobs consumed by app.regulatory.services.signal_service.
    # Phase 26 Option 1A defaults: 0-7d full weight, linear decay to 0 over
    # 7..15d, ignored beyond 15d. (Legacy values 5/3 are auto-upgraded below.)
    'signal_max_age_days': '15',
    'signal_decay_days': '8',
    'signal_max_boost': '8.0',            # ±points on the 0-100 composite score
    'signal_min_dollar_value': '25000',   # below this notional value -> tiny weight
    'signal_strong_dollar_value': '1000000',  # at/above this -> full weight
}

# Settings whose old defaults should be force-upgraded to the new defaults if
# unchanged from the legacy value (Phase 26 migration). If the user has
# explicitly set a different value, we leave it alone.
_LEGACY_UPGRADES = {
    'signal_max_age_days': ('5', '15'),
    'signal_decay_days': ('3', '8'),
}

async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        # Phase 26.8: WAL + busy_timeout so Windows AV scanners holding the
        # DB file briefly don't crash any callers with "database is locked".
        try:
            await db.execute('PRAGMA journal_mode = WAL')
            await db.execute('PRAGMA busy_timeout = 10000')
            await db.execute('PRAGMA synchronous = NORMAL')
        except aiosqlite.Error:
            pass
        await db.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            await db.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))
        # Phase 26 migration: upgrade legacy default values to the new Option 1A
        # defaults ONLY if the row still equals the legacy default (user hasn't
        # customized). Leaves operator overrides intact.
        for key, (old_default, new_default) in _LEGACY_UPGRADES.items():
            await db.execute(
                'UPDATE settings SET value = ?, updated_at = CURRENT_TIMESTAMP '
                'WHERE key = ? AND value = ?',
                (new_default, key, old_default),
            )
        # Phase 26.13 migration: purge "phantom" filing rows produced by the
        # pre-26.13 Form 4 parser. These rows came from <nonDerivativeHolding>
        # / <derivativeHolding> elements (post-transaction holdings, NOT
        # actual transactions) and ended up with NULL shares / price /
        # transaction_code. They contaminated the signal classifier's
        # "freshest event" pick, making every multi-row Form 4 render as
        # "$0.00M notional" in the dashboard even when the real notional
        # was >$1M. Safe to delete: no useful information was ever stored
        # on these rows.
        try:
            cur = await db.execute(
                "DELETE FROM filings "
                "WHERE shares IS NULL AND price_per_share IS NULL "
                "AND transaction_code IS NULL AND form IN ('4', '5')"
            )
            await db.commit()
            deleted = cur.rowcount or 0
            if deleted > 0:
                import logging as _log
                _log.getLogger('app.regulatory.db').info(
                    'phase 26.13 migration: purged %d phantom holding-only filing rows',
                    deleted,
                )
        except aiosqlite.Error:
            pass
        await db.commit()
        # Phase 26.50 P2: refresh SQLite's stat tables so the query planner
        # has up-to-date row counts and index selectivity for every query
        # we run.  ANALYZE is cheap (low ms even on multi-MB DBs) and pays
        # for itself many times over for the larger filings/awards tables.
        try:
            await db.execute('ANALYZE')
            await db.commit()
        except aiosqlite.Error:
            # Non-fatal: stale stats only hurt query planning, never
            # correctness.  Log silently — startup must not fail because
            # the optimiser hint refresh hit a snag.
            pass
