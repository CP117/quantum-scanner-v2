
import json
from pathlib import Path
from app.services.crypto_provider_service import fetch_coingecko_catalog, refresh_coingecko_catalog_in_background

DATAFILE = Path(__file__).resolve().parent.parent.parent / 'data' / 'cached_universe.json'
CRYPTO_DATAFILE = Path(__file__).resolve().parent.parent.parent / 'data' / 'cached_crypto_universe.json'
# Phase 26.38 — variant config (leveraged-ETFs-only build).
# If `app/data/variant.json` is present and declares
# `{"universe_mode": "leveraged"}`, the stocks universe is loaded
# exclusively from `data/leveraged_universe.json` instead of the full
# NASDAQ/NYSE listing.  This is how the "leveraged-only" distributable
# zip artifact targets ~270 symbols instead of ~12,000.
# The variant file is INTENTIONALLY ABSENT from the main app build —
# its presence is exactly what marks a leveraged-only install.
VARIANT_FILE = Path(__file__).resolve().parent.parent / 'data' / 'variant.json'
LEVERAGED_DATAFILE = Path(__file__).resolve().parent.parent.parent / 'data' / 'leveraged_universe.json'
_universe_cache = None
_crypto_universe_cache = None
_variant_cache: dict | None = None


def _load_variant_config() -> dict:
    """Return the parsed variant config, or `{}` for the main (full-universe)
    build.  Cached after first read because the file never changes at
    runtime — operators rebuild the zip to switch variants."""
    global _variant_cache
    if _variant_cache is not None:
        return _variant_cache
    try:
        if VARIANT_FILE.exists():
            _variant_cache = json.loads(VARIANT_FILE.read_text(encoding='utf-8')) or {}
        else:
            _variant_cache = {}
    except Exception:  # noqa: BLE001 — bad JSON shouldn't kill the app
        import logging
        logging.getLogger('app.universe').exception('failed to parse variant.json — falling back to full-universe mode')
        _variant_cache = {}
    return _variant_cache


def is_leveraged_variant() -> bool:
    """True when this install is the leveraged-only distributable."""
    return (_load_variant_config().get('universe_mode') == 'leveraged')


def is_crypto_disabled() -> bool:
    """True when the variant config explicitly disables crypto scanning.
    The leveraged-only build sets this to spare CPU + provider quota."""
    return bool(_load_variant_config().get('disable_crypto'))


def _load_leveraged_universe() -> list[dict]:
    """Load the curated leveraged & inverse ETF universe.  Pure local
    JSON read — no network calls.  Also performs a best-effort expansion
    by scanning the bundled NASDAQ listing for additional ticker names
    matching leveraged-ETF keywords (Direxion / ProShares / 3X / 2X /
    Ultra / Bear / Bull / Inverse).  Missing matches don't fail the
    load — the curated baseline is always the safety net."""
    import logging
    log = logging.getLogger('app.universe')
    try:
        base = json.loads(LEVERAGED_DATAFILE.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        log.exception('failed to load leveraged_universe.json: %s', exc)
        base = []
    by_symbol: dict[str, dict] = {r['symbol'].upper(): dict(r) for r in base if r.get('symbol')}

    # Best-effort dynamic expansion: scan the bundled NASDAQ listing for
    # additional leveraged-ETF names by keyword match.  Completely
    # offline — no HTTP calls.  Failures are silent (we always have the
    # curated baseline above).
    try:
        from app.services.universe_extras import get_nasdaq_listing_rows
        keyword_re = None
        import re
        keyword_re = re.compile(
            r'(?i)\b(?:3X|2X|1\.5X|ULTRA(?:PRO|SHORT)?|ULTRAPRO|DIREXION|PROSHARES|GRANITESHARES|MICROSECTORS|TRADR|T-REX|DEFIANCE|VOLATILITY SHARES|LEVERAG(?:E|ED)|INVERSE|BULL|BEAR|DAILY (?:LONG|SHORT))\b'
        )
        for r in get_nasdaq_listing_rows():
            sym = (r.get('symbol') or '').upper()
            name = r.get('name') or ''
            if not sym or sym in by_symbol:
                continue
            if keyword_re.search(name):
                by_symbol[sym] = r
    except Exception:  # noqa: BLE001
        pass

    # User-added symbols still win (lets the user pin extras manually).
    try:
        from app.services.universe_extras import get_user_added
        for r in get_user_added():
            sym = (r.get('symbol') or '').upper()
            if sym:
                by_symbol[sym] = {**by_symbol.get(sym, {}), **r}
    except Exception:  # noqa: BLE001
        pass

    rows = sorted(by_symbol.values(), key=lambda r: r.get('symbol', ''))
    log.info('leveraged universe loaded: %d symbols (curated baseline + NASDAQ-name pattern expansion)', len(rows))
    return rows

def _sanitize_crypto_rows(rows):
    """Filter out unscannable coin rows.

    CoinGecko's `/coins/list` returns ~17k entries including a long tail of
    dead/scam/duplicate-symbol tokens — many with whitespace, unicode, or
    pure-numeric "symbols" that no provider can resolve.  We keep only rows
    that look like real, tradeable tickers so the universe stays useful.
    """
    import re
    # ALLOWED: A-Z, 0-9, and the limited punctuation real tickers use (`.`/`_`).
    # The leading char must be a letter so we drop tokens like ".XYZ-USD" or
    # "1-USD".  Length 2-12 covers BTC, USDT, MATIC, etc. without admitting
    # absurdly long meme tickers.
    valid_core = re.compile(r'^[A-Z][A-Z0-9._]{1,11}$')
    cleaned = []
    for row in rows or []:
        sym = str((row or {}).get('symbol') or '').upper().strip()
        cid = str((row or {}).get('coingecko_id') or '').strip()
        if not sym.endswith('-USD') or not cid:
            continue
        core = sym[:-4]  # strip -USD
        if not valid_core.match(core):
            continue
        if sym.startswith('CGX') or cid.startswith('cgx-'):
            continue
        cleaned.append(row)
    return cleaned

def load_universe(market: str = 'stocks'):
    """Phase 26.66 — returns the UNION of currently-active universe groups.

    Stocks: union of active stock groups (Leveraged ETFs + per-exchange
    shards).  Crypto: union of active crypto groups (Core + Extended), or
    an empty list when no crypto group is active.  See the universe-group
    section near the bottom of this module for the group catalog, the
    active-set persistence, and the build functions.
    """
    if market == 'crypto':
        return build_active_crypto_universe()
    return build_active_stock_universe()


def bust_universe_cache() -> None:
    """Force load_universe to re-merge baseline + NASDAQ + user-added rows on next call."""
    global _universe_cache, _full_stock_catalog_cache, _stock_group_cache
    _universe_cache = None
    _full_stock_catalog_cache = None
    _stock_group_cache = None


def bust_crypto_universe_cache() -> None:
    """Force load_universe('crypto') to re-merge catalog + coin_list on next call.

    Wired into refresh_coingecko_catalog_in_background()'s on-disk write so a
    successful background refresh transparently surfaces any newly-ranked or
    newly-listed coins without requiring a server restart.
    """
    global _crypto_universe_cache, _crypto_catalog_cache, _crypto_group_cache
    _crypto_universe_cache = None
    _crypto_catalog_cache = None
    _crypto_group_cache = None

def get_universe(market: str = 'stocks'):
    return load_universe(market)

def search_universe(query: str, limit: int = 20, market: str = 'both'):
    """Return rows whose symbol OR name matches `query` (case-insensitive
    substring).  `market` may be 'stocks', 'crypto', or 'both' — 'both'
    merges the two universes so a user typing "BTC" or "AAPL" gets a
    hit regardless of which tab they're on.

    Ranking (best-match first):
        1. exact symbol match             (BTC → BTC-USD)
        2. symbol starts-with the query   (AAP → AAPL)
        3. symbol contains the query      (BTC → ABTC-USD)
        4. name starts-with the query
        5. name contains the query
    Ties are broken by market-cap rank (crypto) or alphabetic (stocks).
    """
    q = (query or '').strip().lower()
    if not q:
        return []
    markets = ('stocks', 'crypto') if market == 'both' else (market,)
    scored: list[tuple[int, int, dict]] = []  # (rank, tiebreak, row)
    seen: set[str] = set()
    for mkt in markets:
        for row in load_universe(mkt):
            sym_upper = (row.get('symbol') or '').upper()
            if not sym_upper or sym_upper in seen:
                continue
            symbol = sym_upper.lower()
            name = (row.get('name') or '').lower()
            # Strip common suffixes to make crypto-pair matching feel
            # natural: "BTC" should match "BTC-USD".
            symbol_core = symbol[:-4] if symbol.endswith('-usd') else symbol
            if q == symbol_core or q == symbol:
                rank = 0
            elif symbol_core.startswith(q):
                rank = 1
            elif q in symbol_core or q in symbol:
                rank = 2
            elif name.startswith(q):
                rank = 3
            elif q in name:
                rank = 4
            else:
                continue
            tiebreak = int(row.get('market_cap_rank') or 999999)
            scored.append((rank, tiebreak, {**row, 'market': mkt}))
            seen.add(sym_upper)
    scored.sort(key=lambda x: (x[0], x[1], x[2].get('symbol') or ''))
    return [row for _, _, row in scored[:limit]]

def get_symbol_identity(symbol: str, market: str = 'stocks'):
    target = (symbol or '').strip().upper()
    for row in load_universe(market):
        if row.get('symbol', '').upper() == target:
            return row
    return {'symbol': target, 'name': '', 'exchange': 'unknown'}


def is_supported_provider_symbol(symbol: str) -> bool:
    s = (symbol or '').strip().upper()
    if not s:
        return False
    unsupported_tokens = ['.W', '.U', '.R', '.WS', '.UN', '.RT']
    if any(token in s for token in unsupported_tokens):
        return False
    if s.count('.') > 1:
        return False
    return True


def filter_supported_provider_rows(rows):
    return [row for row in rows if is_supported_provider_symbol(row.get('symbol', ''))]


# ===========================================================================
# Phase 26.66 — toggleable universe groups.
#
# The full stock universe (~11k symbols) and the crypto catalog are chopped
# into independently activatable GROUPS so the user can scan a bounded slice
# at a time instead of the whole listing:
#   * Leveraged & Inverse ETFs (the curated leveraged set)          — 1 group
#   * Each exchange (NASDAQ / NYSE / NYSE Arca / NYSE American) split into
#     evenly-sized shards of ≤ _SHARD_TARGET symbols each.
#   * Crypto — Core pairs   (BTC/ETH/SOL/DOGE/… everyday majors)
#   * Crypto — Extended set (every other ranked CoinGecko coin)
# Active groups persist to data/active_universes.json.  Defaults:
#   leveraged build → only {'leveraged'} active (crypto OFF);
#   full build      → all stock groups + both crypto groups active.
# ===========================================================================
_SHARD_TARGET = 600
_CRYPTO_SHARD_TARGET = 500  # size of each `crypto_rest_i_n` block
_EXCHANGE_GROUPS = [
    ('NASDAQ', 'nasdaq', 'NASDAQ'),
    ('NYSE', 'nyse', 'NYSE'),
    ('NYSE ARCA', 'nyse_arca', 'NYSE Arca'),
    ('NYSE AMERICAN', 'nyse_american', 'NYSE American'),
]
# Everyday "core" crypto pairs (matched on the symbol core, sans -USD).
_CRYPTO_CORE_SYMBOLS = {
    'BTC', 'ETH', 'USDT', 'BNB', 'SOL', 'XRP', 'USDC', 'ADA', 'DOGE', 'TRX',
    'TON', 'AVAX', 'SHIB', 'DOT', 'LINK', 'BCH', 'NEAR', 'MATIC', 'LTC', 'UNI',
    'ICP', 'ETC', 'XLM', 'ATOM', 'XMR', 'FIL', 'HBAR', 'APT', 'ARB', 'OP',
    'VET', 'AAVE', 'INJ', 'PEPE', 'SUI', 'RNDR', 'IMX', 'TAO', 'GRT', 'ALGO',
    'FTM', 'SAND', 'MANA', 'AXS', 'EOS', 'FLOW', 'XTZ', 'CHZ', 'CRV', 'MKR',
    'LDO', 'WIF', 'BONK', 'FET', 'SEI', 'STX', 'RUNE', 'QNT', 'EGLD', 'THETA',
}

ACTIVE_FILE = DATAFILE.parent / 'active_universes.json'
BASELINE_FILE = DATAFILE.parent / 'active_universes.baseline.json'
# Schema version — bumped whenever the on-disk layout of active_universes.json
# gains new fields.  Older files without this tag are auto-migrated in place
# by `_load_active_state`.  Prevents silent data loss when the file is
# rewritten by a stale build that doesn't understand the newer schema.
ACTIVE_SCHEMA_VERSION = 2
# Integrity guardrails — if the persisted active state contains fewer groups
# than this on load, we assume something reset/truncated the file and try
# to restore from the baseline (if present) OR the DEFAULTS.  Detects the
# "empty universe" regression the user hit on 2026-07-02.
_MIN_STOCK_GROUPS_HEALTHY = 3
_MIN_CRYPTO_GROUPS_HEALTHY = 1
_full_stock_catalog_cache: list[dict] | None = None
_crypto_catalog_cache: list[dict] | None = None
_stock_group_cache: list[dict] | None = None
_crypto_group_cache: list[dict] | None = None
_active_state_cache: dict | None = None


def _load_full_stock_catalog() -> list[dict]:
    """Full merged stock universe (cached_universe baseline + NASDAQ listing
    + user-added).  Cached after first build."""
    global _full_stock_catalog_cache
    if _full_stock_catalog_cache is not None:
        return _full_stock_catalog_cache
    import logging
    log = logging.getLogger('app.universe')
    try:
        base = json.loads(DATAFILE.read_text(encoding='utf-8'))
    except FileNotFoundError:
        log.warning('data/cached_universe.json is missing — falling back to NASDAQ listing only.')
        base = []
    except Exception as exc:  # noqa: BLE001
        log.exception('failed to load cached_universe.json: %s', exc)
        base = []
    by_symbol: dict[str, dict] = {r['symbol'].upper(): dict(r) for r in base if r.get('symbol')}
    try:
        from app.services.universe_extras import get_nasdaq_listing_rows
        for r in get_nasdaq_listing_rows():
            sym = (r.get('symbol') or '').upper()
            if sym and sym not in by_symbol:
                by_symbol[sym] = r
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.services.universe_extras import get_user_added
        for r in get_user_added():
            sym = (r.get('symbol') or '').upper()
            if sym:
                by_symbol[sym] = {**by_symbol.get(sym, {}), **r}
    except Exception:  # noqa: BLE001
        pass
    _full_stock_catalog_cache = sorted(by_symbol.values(), key=lambda r: r.get('symbol', ''))
    return _full_stock_catalog_cache


def _load_full_crypto_catalog() -> list[dict]:
    """Full ranked crypto catalog (merged CoinGecko caches).  Mirrors the
    pre-26.66 crypto branch of load_universe but is no longer gated by the
    variant's disable_crypto flag — crypto availability is now controlled by
    the active crypto GROUPS instead."""
    global _crypto_catalog_cache
    if _crypto_catalog_cache is not None:
        return _crypto_catalog_cache
    cached_dynamic: list[dict] = []
    cached_coin_list: list[dict] = []
    try:
        data_dir = Path(__file__).resolve().parent.parent.parent / 'data'
        cache_file = data_dir / 'coingecko_catalog_cache.json'
        coin_list_file = data_dir / 'coingecko_coin_list_cache.json'
        if cache_file.exists():
            payload = json.loads(cache_file.read_text(encoding='utf-8'))
            cached_dynamic = _sanitize_crypto_rows(payload.get('rows') or [])
        if coin_list_file.exists():
            coin_payload = json.loads(coin_list_file.read_text(encoding='utf-8'))
            cached_coin_list = _sanitize_crypto_rows(coin_payload.get('rows') or [])
    except Exception:  # noqa: BLE001
        cached_dynamic = []
        cached_coin_list = []
    merged: dict[str, dict] = {}
    for row in cached_coin_list:
        sym = (row.get('symbol') or '').upper()
        if sym:
            merged[sym] = dict(row)
    for row in cached_dynamic:
        sym = (row.get('symbol') or '').upper()
        if not sym:
            continue
        merged[sym] = {**(merged.get(sym) or {}), **row}
    # Hard-seed the top majors so the ranked set is NEVER empty even if
    # the on-disk cache was degraded by a partial CoinGecko fetch during
    # a rate-limit event.  Only fills in the rank if the disk copy is
    # missing one — real fresh ranks always win.  See
    # `crypto_provider_service._TOP_40_MAJORS`.
    try:
        from app.services.crypto_provider_service import _TOP_40_MAJORS
        for idx, (maj_sym, maj_name, maj_cid) in enumerate(_TOP_40_MAJORS):
            canonical_rank = idx + 1
            row = merged.get(maj_sym) or {}
            existing_rank = int(row.get('market_cap_rank') or 999999)
            if existing_rank > canonical_rank:
                merged[maj_sym] = {
                    'symbol': maj_sym,
                    'name': row.get('name') or maj_name,
                    'exchange': 'CRYPTO',
                    'coingecko_id': maj_cid,
                    'market_cap_rank': canonical_rank,
                }
        # Also promote every _CRYPTO_CORE_SYMBOLS entry: if a symbol's
        # coingecko_id is known via the on-disk coin_list, give it a
        # synthetic rank right after the top-40 block.  Guarantees the
        # `crypto_core` group has all ~60 hardcoded majors even during
        # provider outages.
        _top40_syms = {s for s, _, _ in _TOP_40_MAJORS}
        rank_cursor = len(_TOP_40_MAJORS)
        for core_ticker in sorted(_CRYPTO_CORE_SYMBOLS):
            sym = f'{core_ticker}-USD'
            if sym in _top40_syms:
                continue
            row = merged.get(sym) or {}
            existing_rank = int(row.get('market_cap_rank') or 999999)
            cid = row.get('coingecko_id')
            if not cid:
                # No coin_list entry either — skip; we can't fetch quotes for it.
                continue
            rank_cursor += 1
            if existing_rank > rank_cursor:
                merged[sym] = {
                    'symbol': sym,
                    'name': row.get('name') or core_ticker,
                    'exchange': 'CRYPTO',
                    'coingecko_id': cid,
                    'market_cap_rank': rank_cursor,
                }
    except Exception:  # noqa: BLE001 — belt-and-suspenders; never block the load
        pass
    ranked = [row for row in merged.values() if int(row.get('market_cap_rank') or 999999) < 999999]
    if ranked:
        catalog = sorted(ranked, key=lambda r: (int(r.get('market_cap_rank') or 999999), (r.get('symbol') or '')))
        refresh_coingecko_catalog_in_background()
    elif merged:
        catalog = sorted(merged.values(), key=lambda r: (r.get('symbol') or ''))
        refresh_coingecko_catalog_in_background()
    else:
        try:
            catalog = _sanitize_crypto_rows(json.loads(CRYPTO_DATAFILE.read_text(encoding='utf-8')))
        except Exception:  # noqa: BLE001
            catalog = []
        refresh_coingecko_catalog_in_background()
    _crypto_catalog_cache = catalog
    return _crypto_catalog_cache


def _crypto_core_of(sym: str) -> str:
    s = (sym or '').upper()
    return s[:-4] if s.endswith('-USD') else s


def _stock_groups() -> list[dict]:
    global _stock_group_cache
    if _stock_group_cache is not None:
        return _stock_group_cache
    import math
    groups: list[dict] = []
    lev = _load_leveraged_universe()
    groups.append({'key': 'leveraged', 'label': 'Leveraged & Inverse ETFs',
                   'exchange': 'Leveraged ETFs', 'market': 'stocks',
                   '_rows': lev, 'count': len(lev)})
    catalog = _load_full_stock_catalog()
    by_exch: dict[str, list[dict]] = {}
    for r in catalog:
        e = str(r.get('exchange') or '').upper()
        by_exch.setdefault(e, []).append(r)
    for exch_key, slug, label in _EXCHANGE_GROUPS:
        rows = sorted(by_exch.get(exch_key, []), key=lambda r: (r.get('symbol') or ''))
        if not rows:
            continue
        n = max(1, math.ceil(len(rows) / _SHARD_TARGET))
        chunk = math.ceil(len(rows) / n)
        for i in range(n):
            shard = rows[i * chunk:(i + 1) * chunk]
            if not shard:
                continue
            groups.append({'key': f'{slug}_{i + 1}_{n}',
                           'label': (f'{label} · part {i + 1}/{n}' if n > 1 else label),
                           'exchange': label, 'market': 'stocks',
                           '_rows': shard, 'count': len(shard)})
    _stock_group_cache = groups
    return groups


def _crypto_groups() -> list[dict]:
    global _crypto_group_cache
    if _crypto_group_cache is not None:
        return _crypto_group_cache
    import math
    catalog = _load_full_crypto_catalog()
    core: list[dict] = []
    rest: list[dict] = []
    for r in catalog:
        if _crypto_core_of(r.get('symbol') or '') in _CRYPTO_CORE_SYMBOLS:
            core.append(r)
        else:
            rest.append(r)
    # Sort `rest` by rank so the "part 1" shard is the highest-ranked
    # (best-signal) coins.  A tiny universe (<= _CRYPTO_SHARD_TARGET) is
    # kept as a single "Extended set" group so the UI doesn't get
    # cluttered with a single-part shard label.
    rest.sort(key=lambda r: (int(r.get('market_cap_rank') or 999999),
                             (r.get('symbol') or '')))
    groups: list[dict] = [
        {'key': 'crypto_core', 'label': 'Crypto — Core pairs',
         'exchange': 'Crypto', 'market': 'crypto',
         '_rows': core, 'count': len(core)},
    ]
    if len(rest) <= _CRYPTO_SHARD_TARGET:
        groups.append({'key': 'crypto_rest', 'label': 'Crypto — Extended set',
                       'exchange': 'Crypto', 'market': 'crypto',
                       '_rows': rest, 'count': len(rest)})
    else:
        n = max(1, math.ceil(len(rest) / _CRYPTO_SHARD_TARGET))
        chunk = math.ceil(len(rest) / n)
        for i in range(n):
            shard = rest[i * chunk:(i + 1) * chunk]
            if not shard:
                continue
            groups.append({
                'key':      f'crypto_rest_{i + 1}_{n}',
                'label':    f'Crypto — Extended · part {i + 1}/{n}',
                'exchange': 'Crypto', 'market': 'crypto',
                '_rows':    shard,
                'count':    len(shard),
            })
    _crypto_group_cache = groups
    return _crypto_group_cache


def _default_active_stock_keys() -> set[str]:
    keys = {'leveraged'}
    if not is_leveraged_variant():
        keys |= {g['key'] for g in _stock_groups()}
    return keys


def _default_active_crypto_keys() -> set[str]:
    if is_leveraged_variant():
        return set()
    # All crypto groups active by default — includes crypto_core plus
    # every crypto_rest_i_n shard.  Reads from the freshly-built group
    # list so this stays in sync when the shard count changes.
    return {g['key'] for g in _crypto_groups()}


def _expand_legacy_crypto_rest(active_crypto: set[str] | None) -> set[str] | None:
    """Migrate a stored ``'crypto_rest'`` key to the new sharded
    ``crypto_rest_i_n`` group keys so users who saved their selection
    before the shard split don't silently lose the extended set."""
    if not active_crypto:
        return active_crypto
    if 'crypto_rest' not in active_crypto:
        return active_crypto
    migrated = set(active_crypto)
    migrated.discard('crypto_rest')
    for g in _crypto_groups():
        if g['key'].startswith('crypto_rest'):
            migrated.add(g['key'])
    return migrated


def _load_active_state() -> dict:
    global _active_state_cache
    if _active_state_cache is not None:
        return _active_state_cache
    import logging
    log = logging.getLogger('app.universe')
    raw: dict = {}
    try:
        if ACTIVE_FILE.exists():
            raw = json.loads(ACTIVE_FILE.read_text(encoding='utf-8')) or {}
    except Exception:  # noqa: BLE001
        raw = {}
    stocks = set(raw['stocks']) if isinstance(raw.get('stocks'), list) else None
    crypto = set(raw['crypto']) if isinstance(raw.get('crypto'), list) else None
    schema_version = int(raw.get('schema_version') or 0) if isinstance(raw, dict) else 0

    # ---- Integrity guardrail (Phase 26.68) --------------------------------
    # If the persisted state was truncated (e.g. an older build's writer
    # wiped it, or the file got corrupted), auto-restore from the baseline
    # snapshot when available.  This is the safety net that prevents the
    # "leveraged/crypto scan universes empty" regression from silently
    # persisting across restarts.
    stocks_low = stocks is not None and len(stocks) < _MIN_STOCK_GROUPS_HEALTHY
    crypto_low = crypto is not None and len(crypto) < _MIN_CRYPTO_GROUPS_HEALTHY and not is_leveraged_variant()
    if stocks_low or crypto_low:
        restored = _load_baseline_snapshot()
        if restored:
            if stocks_low and restored.get('stocks'):
                log.warning('active_universes.json stocks list truncated (%d groups) — restoring %d from baseline',
                            len(stocks or ()), len(restored['stocks']))
                stocks = set(restored['stocks'])
            if crypto_low and restored.get('crypto'):
                log.warning('active_universes.json crypto list truncated (%d groups) — restoring %d from baseline',
                            len(crypto or ()), len(restored['crypto']))
                crypto = set(restored['crypto'])
    if schema_version < ACTIVE_SCHEMA_VERSION and raw:
        log.info('active_universes.json schema v%d -> v%d (upgrading)',
                 schema_version, ACTIVE_SCHEMA_VERSION)

    # Migrate the pre-shard `crypto_rest` key to the new per-shard keys.
    crypto = _expand_legacy_crypto_rest(crypto)

    _active_state_cache = {
        'stocks': stocks,
        'crypto': crypto,
        'schema_version': ACTIVE_SCHEMA_VERSION,
    }
    return _active_state_cache


def _load_baseline_snapshot() -> dict | None:
    """Read the baseline snapshot of active universes (last known-good set).
    Written by `_save_baseline_snapshot` whenever the current active state
    is confirmed healthy (>= HEALTHY thresholds).  Absent on first boot."""
    try:
        if BASELINE_FILE.exists():
            payload = json.loads(BASELINE_FILE.read_text(encoding='utf-8'))
            return {
                'stocks': list(payload.get('stocks') or []),
                'crypto': list(payload.get('crypto') or []),
            }
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger('app.universe').exception('failed to read active_universes.baseline.json')
    return None


def _save_baseline_snapshot(stocks: set, crypto: set) -> None:
    """Only called after a healthy active-state save — captures a known-good
    superset that the integrity guardrail can restore from."""
    if len(stocks or ()) < _MIN_STOCK_GROUPS_HEALTHY:
        return
    try:
        BASELINE_FILE.write_text(json.dumps({
            'schema_version': ACTIVE_SCHEMA_VERSION,
            'stocks': sorted(stocks),
            'crypto': sorted(crypto),
            'captured_at_utc': __import__('datetime').datetime.now(
                __import__('datetime').timezone.utc).isoformat(),
        }, indent=2), encoding='utf-8')
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger('app.universe').exception('failed to persist active_universes.baseline.json')


def _save_active_state() -> None:
    st = _load_active_state()
    stocks = st.get('stocks') or set()
    crypto = st.get('crypto') or set()
    try:
        ACTIVE_FILE.write_text(json.dumps({
            'schema_version': ACTIVE_SCHEMA_VERSION,
            'stocks': sorted(stocks),
            'crypto': sorted(crypto),
            'updated_at_utc': __import__('datetime').datetime.now(
                __import__('datetime').timezone.utc).isoformat(),
        }, indent=2), encoding='utf-8')
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger('app.universe').exception('failed to persist active_universes.json')
    # If the just-saved state is healthy, refresh the baseline snapshot so
    # a future truncation can be auto-restored.
    _save_baseline_snapshot(stocks, crypto)


def universe_integrity_status() -> dict:
    """Diagnostic: exposes the current active-universe state + baseline
    health for the Metrics Hub / admin surfaces."""
    st = _load_active_state()
    stocks = st.get('stocks') or set()
    crypto = st.get('crypto') or set()
    baseline = _load_baseline_snapshot() or {}
    return {
        'schema_version': ACTIVE_SCHEMA_VERSION,
        'stocks_active': len(stocks),
        'crypto_active': len(crypto),
        'stocks_healthy': len(stocks) >= _MIN_STOCK_GROUPS_HEALTHY,
        'crypto_healthy': (len(crypto) >= _MIN_CRYPTO_GROUPS_HEALTHY) or is_leveraged_variant(),
        'baseline_present': BASELINE_FILE.exists(),
        'baseline_stocks': len(baseline.get('stocks') or []),
        'baseline_crypto': len(baseline.get('crypto') or []),
        'min_stock_groups_healthy': _MIN_STOCK_GROUPS_HEALTHY,
        'min_crypto_groups_healthy': _MIN_CRYPTO_GROUPS_HEALTHY,
    }


def get_active_keys(market: str) -> set[str]:
    st = _load_active_state()
    cur = st.get(market)
    if cur is None:
        cur = (_default_active_stock_keys() if market == 'stocks'
               else _default_active_crypto_keys())
        st[market] = set(cur)
    valid = {g['key'] for g in (_stock_groups() if market == 'stocks' else _crypto_groups())}
    return {k for k in cur if k in valid}


def is_crypto_active() -> bool:
    """True when ≥1 crypto group is active.  Cheap — does NOT load the
    crypto catalog, so it's safe to call from the scan loop hot path."""
    st = _load_active_state()
    cur = st.get('crypto')
    if cur is None:
        cur = _default_active_crypto_keys()
    return len(cur) > 0


def set_group_active(market: str, key: str, active: bool) -> dict:
    market = 'crypto' if market == 'crypto' else 'stocks'
    st = _load_active_state()
    cur = set(get_active_keys(market))
    valid = {g['key'] for g in (_stock_groups() if market == 'stocks' else _crypto_groups())}
    if key not in valid:
        return {'ok': False, 'error': f'unknown universe group: {key}',
                'market': market, 'active_keys': sorted(cur)}
    if active:
        cur.add(key)
    else:
        cur.discard(key)
    st[market] = cur
    _save_active_state()
    return {'ok': True, 'market': market, 'key': key, 'active': active,
            'active_keys': sorted(cur)}


def list_universe_groups(market: str = 'stocks') -> list[dict]:
    groups = _stock_groups() if market != 'crypto' else _crypto_groups()
    active = get_active_keys(market)
    return [{'key': g['key'], 'label': g['label'], 'exchange': g['exchange'],
             'market': g['market'], 'count': g['count'], 'active': g['key'] in active}
            for g in groups]


def build_active_stock_universe() -> list[dict]:
    active = get_active_keys('stocks')
    if not active:
        return []
    by_sym: dict[str, dict] = {}
    for g in _stock_groups():
        if g['key'] not in active:
            continue
        for r in g['_rows']:
            sym = (r.get('symbol') or '').upper()
            if sym and sym not in by_sym:
                by_sym[sym] = r
    return sorted(by_sym.values(), key=lambda r: r.get('symbol', ''))


def build_active_crypto_universe() -> list[dict]:
    if not is_crypto_active():
        return []
    active = get_active_keys('crypto')
    if not active:
        return []
    by_sym: dict[str, dict] = {}
    for g in _crypto_groups():
        if g['key'] not in active:
            continue
        for r in g['_rows']:
            sym = (r.get('symbol') or '').upper()
            if sym and sym not in by_sym:
                by_sym[sym] = r
    return sorted(by_sym.values(),
                  key=lambda r: (int(r.get('market_cap_rank') or 999999), (r.get('symbol') or '')))
