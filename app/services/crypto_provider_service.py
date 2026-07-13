from __future__ import annotations
import json
import threading
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone
import requests
from app.config import settings
from app.services.provider_session import mark_provider_failure

DATA_DIR = Path(__file__).resolve().parent.parent.parent / 'data'
CATALOG_FILE = DATA_DIR / 'coingecko_catalog_cache.json'
COIN_LIST_FILE = DATA_DIR / 'coingecko_coin_list_cache.json'

STATIC_FALLBACK_MAP = {
    'BTC-USD': 'bitcoin', 'ETH-USD': 'ethereum', 'SOL-USD': 'solana', 'XRP-USD': 'ripple', 'BNB-USD': 'binancecoin',
    'DOGE-USD': 'dogecoin', 'ADA-USD': 'cardano', 'AVAX-USD': 'avalanche-2', 'LINK-USD': 'chainlink', 'DOT-USD': 'polkadot',
    'MATIC-USD': 'matic-network', 'LTC-USD': 'litecoin', 'BCH-USD': 'bitcoin-cash', 'UNI-USD': 'uniswap', 'ATOM-USD': 'cosmos',
    'ETC-USD': 'ethereum-classic', 'ICP-USD': 'internet-computer', 'FIL-USD': 'filecoin', 'APT-USD': 'aptos', 'ARB-USD': 'arbitrum',
    'NEAR-USD': 'near', 'OP-USD': 'optimism', 'HBAR-USD': 'hedera-hashgraph', 'VET-USD': 'vechain', 'INJ-USD': 'injective-protocol'
}
_catalog_cache = None
_symbol_to_id = None


def _utcnow():
    return datetime.now(timezone.utc)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding='utf-8')


def _cache_fresh(payload: dict) -> bool:
    try:
        fetched_at = payload.get('fetched_at')
        if not fetched_at:
            return False
        dt = datetime.fromisoformat(fetched_at.replace('Z', '+00:00'))
        age = (_utcnow() - dt).total_seconds()
        return age <= settings.coingecko_catalog_ttl_seconds
    except Exception:
        return False


# Top-40 majors with canonical (display name, coingecko_id) tuples.  Used to
# anchor the leading positions in the ranked catalog whenever we re-build it
# from a secondary source (e.g. CryptoCompare) that ranks by 24h volume — so
# a memecoin pump doesn't push BTC out of rank #1.  Also used to override
# any catalog row whose coingecko_id was contaminated by a same-ticker
# collision from CoinGecko's `/coins/list`.
_TOP_40_MAJORS: list[tuple[str, str, str]] = [
    ('BTC-USD', 'Bitcoin', 'bitcoin'),
    ('ETH-USD', 'Ethereum', 'ethereum'),
    ('USDT-USD', 'Tether', 'tether'),
    ('XRP-USD', 'XRP', 'ripple'),
    ('BNB-USD', 'BNB', 'binancecoin'),
    ('SOL-USD', 'Solana', 'solana'),
    ('USDC-USD', 'USD Coin', 'usd-coin'),
    ('DOGE-USD', 'Dogecoin', 'dogecoin'),
    ('ADA-USD', 'Cardano', 'cardano'),
    ('AVAX-USD', 'Avalanche', 'avalanche-2'),
    ('TRX-USD', 'TRON', 'tron'),
    ('TON-USD', 'Toncoin', 'the-open-network'),
    ('LINK-USD', 'Chainlink', 'chainlink'),
    ('DOT-USD', 'Polkadot', 'polkadot'),
    ('MATIC-USD', 'Polygon', 'matic-network'),
    ('BCH-USD', 'Bitcoin Cash', 'bitcoin-cash'),
    ('SHIB-USD', 'Shiba Inu', 'shiba-inu'),
    ('LTC-USD', 'Litecoin', 'litecoin'),
    ('UNI-USD', 'Uniswap', 'uniswap'),
    ('ATOM-USD', 'Cosmos', 'cosmos'),
    ('NEAR-USD', 'NEAR Protocol', 'near'),
    ('ETC-USD', 'Ethereum Classic', 'ethereum-classic'),
    ('XLM-USD', 'Stellar', 'stellar'),
    ('APT-USD', 'Aptos', 'aptos'),
    ('FIL-USD', 'Filecoin', 'filecoin'),
    ('ARB-USD', 'Arbitrum', 'arbitrum'),
    ('OP-USD', 'Optimism', 'optimism'),
    ('ICP-USD', 'Internet Computer', 'internet-computer'),
    ('HBAR-USD', 'Hedera', 'hedera-hashgraph'),
    ('VET-USD', 'VeChain', 'vechain'),
    ('AAVE-USD', 'Aave', 'aave'),
    ('XMR-USD', 'Monero', 'monero'),
    ('INJ-USD', 'Injective', 'injective-protocol'),
    ('MKR-USD', 'Maker', 'maker'),
    ('GRT-USD', 'The Graph', 'the-graph'),
    ('ALGO-USD', 'Algorand', 'algorand'),
    ('SUI-USD', 'Sui', 'sui'),
    ('SAND-USD', 'The Sandbox', 'the-sandbox'),
    ('MANA-USD', 'Decentraland', 'decentraland'),
    ('FLOW-USD', 'Flow', 'flow'),
]


def _ranked_from_cryptocompare(max_pages: int = 25) -> list[dict]:
    """Backup ranked-coin source when CoinGecko's /coins/markets is rate-
    limited.  CryptoCompare's /data/top/totalvolfull returns the top-N
    coins by 24-hour USD volume and has much more generous free-tier limits
    (no auth, ~50 req/min).  Returns rows in the same shape produced by
    the CoinGecko path, with `coingecko_id` resolved from the on-disk
    `/coins/list` cache when possible (and the top-40 majors' ids hard-
    overridden so a same-ticker memecoin can't hijack BTC/ETH/etc.).
    """
    out: list[dict] = []
    seen: set[str] = set()
    base_url = 'https://min-api.cryptocompare.com/data/top/totalvolfull'
    try:
        for page in range(0, max_pages):
            res = requests.get(
                base_url,
                params={'limit': 100, 'tsym': 'USD', 'page': page},
                timeout=settings.provider_timeout_seconds,
                headers={'User-Agent': 'market-refinement-dashboard/1.0'},
            )
            if res.status_code != 200:
                break
            data = res.json() or {}
            if (data.get('Response') or '').upper() == 'ERROR':
                break
            items = data.get('Data') or []
            if not items:
                break
            for item in items:
                coin = item.get('CoinInfo') or {}
                sym = (coin.get('Name') or coin.get('Internal') or '').upper().strip()
                full_name = coin.get('FullName') or sym
                display = full_name.split(' (')[0] if ' (' in full_name else full_name
                if not sym:
                    continue
                full_sym = f'{sym}-USD'
                if full_sym in seen:
                    continue
                seen.add(full_sym)
                out.append({
                    'symbol': full_sym,
                    'name': display,
                    'exchange': 'CRYPTO',
                    'coingecko_id': (coin.get('Internal') or sym).lower(),
                    'market_cap_rank': len(out) + 1,
                })
    except Exception as exc:
        mark_provider_failure(str(exc))

    if not out:
        return out

    # Resolve real coingecko_ids from the on-disk coin_list cache when the
    # symbol is unambiguous, then hard-override the top-40 majors so
    # collisions (e.g. BTC vs "batcat") can never corrupt them.
    coin_list = _read_json(COIN_LIST_FILE)
    cg_by_sym: dict[str, str] = {}
    for row in (coin_list.get('rows') or []):
        sym = row.get('symbol')
        cid = row.get('coingecko_id')
        if sym and cid:
            # Only adopt the cg id if it's unambiguous - first-seen wins.
            cg_by_sym.setdefault(sym, cid)
    known = {sym: (name, cid) for sym, name, cid in _TOP_40_MAJORS}
    # Build a quick lookup so we can also slot the 40 majors at ranks 1-40
    # in their canonical order regardless of where CryptoCompare ranked them.
    by_sym = {r['symbol']: r for r in out}
    for i, (sym, name, cid) in enumerate(_TOP_40_MAJORS, start=1):
        if sym in by_sym:
            by_sym[sym].update({'market_cap_rank': i, 'name': name, 'coingecko_id': cid})
        else:
            by_sym[sym] = {'symbol': sym, 'name': name, 'exchange': 'CRYPTO',
                           'coingecko_id': cid, 'market_cap_rank': i}
    # Re-number anything beyond rank 40 to slot in *after* the anchored majors.
    rest = [r for r in by_sym.values() if r['symbol'] not in known]
    rest.sort(key=lambda r: int(r.get('market_cap_rank') or 999999))
    for offset, r in enumerate(rest, start=41):
        r['market_cap_rank'] = offset
        # Promote real coingecko_id when known.
        if r['symbol'] in cg_by_sym:
            r['coingecko_id'] = cg_by_sym[r['symbol']]
    return list(by_sym.values())


def _symbol_for_market_row(row: dict) -> str:
    symbol = str(row.get('symbol') or '').upper().strip()
    return f'{symbol}-USD' if symbol else ''


def _catalog_rows_from_coin_list() -> list[dict]:
    payload = _read_json(COIN_LIST_FILE)
    if _cache_fresh(payload):
        rows = payload.get('rows') or []
        if rows:
            return rows
    try:
        url = f"{settings.coingecko_base_url}/coins/list?include_platform=false"
        res = requests.get(url, timeout=settings.provider_timeout_seconds, headers={'accept':'application/json','user-agent':'market-refinement-dashboard/1.0'})
        res.raise_for_status()
        raw = res.json() or []
        rows = []
        for item in raw:
            sym = str(item.get('symbol') or '').upper().strip()
            cid = str(item.get('id') or '').strip()
            if not sym or not cid:
                continue
            rows.append({'symbol': f'{sym}-USD', 'name': item.get('name') or sym, 'exchange': 'CRYPTO', 'coingecko_id': cid, 'market_cap_rank': 999999})
        dedup = {}
        for row in rows:
            dedup.setdefault(row['symbol'], row)
        rows = list(dedup.values())
        _write_json(COIN_LIST_FILE, {'fetched_at': _utcnow().isoformat(), 'rows': rows})
        return rows
    except Exception as exc:
        mark_provider_failure(str(exc))
        return payload.get('rows') or []


def fetch_coingecko_catalog(force_refresh: bool = False) -> list[dict]:
    global _catalog_cache, _symbol_to_id
    if _catalog_cache is None:
        _catalog_cache = _read_json(CATALOG_FILE)
    if not force_refresh and _catalog_cache and _cache_fresh(_catalog_cache):
        rows = _catalog_cache.get('rows') or []
        _symbol_to_id = {row['symbol']: row.get('coingecko_id') for row in rows if row.get('symbol') and row.get('coingecko_id')}
        return rows
    ranked_rows = []
    rate_limited = False
    try:
        for page in range(1, settings.coingecko_catalog_pages + 1):
            url = f"{settings.coingecko_base_url}/coins/markets?vs_currency=usd&order=market_cap_desc&per_page={settings.coingecko_catalog_page_size}&page={page}&sparkline=false&price_change_percentage=24h,7d"
            # Retry on the CoinGecko free-tier 429 limit (~10-15 req/min).
            # Without backoff a single 429 ends the whole sweep and we lose
            # every page after it.  Up to 3 retries per page, exponential
            # 35 -> 70 -> 105s waits, then move on if still throttled.
            batch = None
            for attempt in range(3):
                res = requests.get(
                    url,
                    timeout=settings.provider_timeout_seconds,
                    headers={'accept': 'application/json', 'user-agent': 'market-refinement-dashboard/1.0'},
                )
                if res.status_code == 429:
                    rate_limited = True
                    import time as _time
                    _time.sleep(35 * (attempt + 1))
                    continue
                res.raise_for_status()
                batch = res.json() or []
                break
            if batch is None:
                # Exhausted retries on this page — abort the sweep but DO
                # NOT discard pages we already collected.
                break
            if not batch:
                break
            for item in batch:
                sym = _symbol_for_market_row(item)
                if not sym:
                    continue
                ranked_rows.append({'symbol': sym, 'name': item.get('name') or sym, 'exchange': 'CRYPTO', 'coingecko_id': item.get('id') or '', 'market_cap_rank': int(item.get('market_cap_rank') or 999999)})
            # Inter-page throttle: ~12s gives ~5 req/min, comfortably under
            # the free-tier ceiling and lets a 20-page sweep finish in ~4min.
            import time as _time
            _time.sleep(12)
    except Exception as exc:
        mark_provider_failure(str(exc))

    # CRITICAL: if we got ZERO ranked rows (e.g. CoinGecko was unreachable or
    # rate-limited from the very first page), try the CryptoCompare ranked
    # endpoint as a fallback BEFORE giving up.  CryptoCompare has much more
    # generous free-tier limits and can populate ~2,500 ranked coins in
    # ~25 calls.  Only if both providers fail do we preserve the existing
    # on-disk cache instead of overwriting it.
    if not ranked_rows:
        ranked_rows = _ranked_from_cryptocompare()
    else:
        # Phase 22: ALSO top-up with CryptoCompare even when CG returned
        # something.  This bridges the gap when CoinGecko 429s after only
        # a few pages (the user's symptom: "crypto table only populates 10
        # pages of results").  CG-ranked rows win on conflict so the
        # market_cap_rank order is preserved for coins both providers
        # know about.
        try:
            existing_syms = {r.get('symbol') for r in ranked_rows if r.get('symbol')}
            cc_rows = _ranked_from_cryptocompare()
            # CryptoCompare's local rank is 1..N within its own list; we
            # offset by the CG count so the merged ordering keeps CG up
            # top and CC backfill below.
            cg_count = len(ranked_rows)
            added = 0
            for row in cc_rows:
                sym = row.get('symbol')
                if not sym or sym in existing_syms:
                    continue
                # Re-rank: bump CC entries to sit after the CG block but
                # before the unranked 999999 floor.
                row = dict(row)
                row['market_cap_rank'] = cg_count + int(row.get('market_cap_rank') or 0)
                ranked_rows.append(row)
                existing_syms.add(sym)
                added += 1
            if added:
                import logging as _log
                _log.getLogger('app.crypto.catalog').info(
                    'coingecko catalog: topped up with %d cryptocompare rows (cg=%d cc_added=%d)',
                    added, cg_count, added,
                )
        except Exception as exc:
            mark_provider_failure(str(exc))
    if not ranked_rows:
        existing = _read_json(CATALOG_FILE)
        existing_rows = (existing or {}).get('rows') or []
        existing_ranked = [r for r in existing_rows if int(r.get('market_cap_rank') or 999999) < 999999]
        if existing_ranked:
            _catalog_cache = existing
            _symbol_to_id = {row['symbol']: row.get('coingecko_id') for row in existing_rows if row.get('coingecko_id')}
            return existing_rows
        # No prior cache and no ranked rows — fall through to static fallback.

    extended_rows = _catalog_rows_from_coin_list()
    # CRITICAL: ranked rows (from /coins/markets, market-cap ordered) MUST
    # win on ticker collisions.  ~5% of /coins/list tickers collide with a
    # real ranked coin (e.g. memecoin "batcat" shares ticker BTC with
    # Bitcoin).  If the unranked /coins/list row lands in `merged` last it
    # overwrites the authoritative ranked entry's coingecko_id and name —
    # the next CoinGecko snapshot then returns the memecoin's data for
    # BTC-USD.  Iterate extended FIRST, ranked LAST.
    merged = {}
    for row in extended_rows + ranked_rows:
        if row.get('symbol') and row.get('coingecko_id'):
            existing = merged.get(row['symbol']) or {}
            existing_rank = int(existing.get('market_cap_rank') or 999999)
            new_rank = int(row.get('market_cap_rank') or 999999)
            # Prefer the LOWER-ranked (more authoritative) coin's
            # coingecko_id + display name.  Ranked rows always win because
            # they have rank < 999999; ties default to the most recent.
            authoritative = row if new_rank <= existing_rank else existing
            merged[row['symbol']] = {
                'symbol': row['symbol'],
                'name': authoritative.get('name') or row.get('name') or row['symbol'],
                'exchange': 'CRYPTO',
                'coingecko_id': authoritative.get('coingecko_id') or row.get('coingecko_id') or '',
                'market_cap_rank': min(existing_rank, new_rank),
            }
    if not merged:
        merged = {sym: {'symbol': sym, 'name': sym.replace('-USD',''), 'exchange': 'CRYPTO', 'coingecko_id': cid, 'market_cap_rank': 999999} for sym, cid in STATIC_FALLBACK_MAP.items()}
    # Hard-seed the top-40 majors so a partial upstream fetch (CoinGecko
    # 429 after 1 page, CryptoCompare 401) can never eject BTC/ETH/etc.
    # from the ranked set.  Only fill in the slot if it's currently
    # missing OR ranked worse than the authoritative position — ranked
    # data returned by CoinGecko is still preferred when present.
    for idx, (sym, name, cid) in enumerate(_TOP_40_MAJORS):
        canonical_rank = idx + 1
        existing = merged.get(sym) or {}
        existing_rank = int(existing.get('market_cap_rank') or 999999)
        if existing_rank > canonical_rank:
            merged[sym] = {
                'symbol': sym,
                'name': existing.get('name') or name,
                'exchange': 'CRYPTO',
                'coingecko_id': cid,
                'market_cap_rank': canonical_rank,
            }
    rows = sorted(merged.values(), key=lambda r: (int(r.get('market_cap_rank') or 999999), r.get('symbol', '')))

    # Never-shrink guard: if the freshly-built ranked set is materially
    # smaller than what's already on disk (e.g. CoinGecko 429s after 1
    # page and the CryptoCompare fallback is dead behind a paywall), keep
    # the existing on-disk cache.  Overwriting a healthy 6,000-row cache
    # with a partial 250-row response was the cause of the "crypto
    # universe silently shrank to a few hundred coins" regression.
    existing_disk = _read_json(CATALOG_FILE) or {}
    existing_rows = (existing_disk.get('rows') or [])
    existing_ranked_count = sum(1 for r in existing_rows if int(r.get('market_cap_rank') or 999999) < 999999)
    new_ranked_count = sum(1 for r in rows if int(r.get('market_cap_rank') or 999999) < 999999)
    SHRINK_FLOOR = 200  # below this we treat the disk copy as untrusted
    if (existing_ranked_count >= SHRINK_FLOOR
            and new_ranked_count < max(SHRINK_FLOOR, int(existing_ranked_count * 0.5))):
        import logging as _log
        _log.getLogger('app.crypto.catalog').warning(
            'coingecko catalog refresh: keeping on-disk cache '
            '(new_ranked=%d < existing_ranked=%d/2) — providers likely throttled',
            new_ranked_count, existing_ranked_count,
        )
        _catalog_cache = existing_disk
        _symbol_to_id = {row['symbol']: row.get('coingecko_id')
                         for row in existing_rows if row.get('coingecko_id')}
        return existing_rows

    _write_json(CATALOG_FILE, {'fetched_at': _utcnow().isoformat(), 'rows': rows})
    _catalog_cache = {'fetched_at': _utcnow().isoformat(), 'rows': rows}
    _symbol_to_id = {row['symbol']: row.get('coingecko_id') for row in rows if row.get('coingecko_id')}
    return rows


def coin_id_for_symbol(symbol: str) -> str | None:
    global _symbol_to_id
    s = (symbol or '').strip().upper()
    if _symbol_to_id is None:
        fetch_coingecko_catalog(False)
    return (_symbol_to_id or {}).get(s) or STATIC_FALLBACK_MAP.get(s)


def fetch_coingecko_snapshots(symbols: list[str]) -> dict[str, dict]:
    wanted = []
    mapping = {}
    for sym in symbols or []:
        cid = coin_id_for_symbol(sym)
        if cid:
            wanted.append(cid)
            mapping[cid] = sym
    if not wanted:
        return {}
    try:
        ids = ','.join(sorted(set(wanted)))
        url = f"{settings.coingecko_base_url}/coins/markets?vs_currency=usd&ids={urllib.parse.quote(ids)}&price_change_percentage=24h,7d"
        res = requests.get(url, timeout=settings.provider_timeout_seconds, headers={'accept': 'application/json', 'user-agent': 'market-refinement-dashboard/1.0'})
        res.raise_for_status()
        rows = res.json() or []
        out = {}
        for row in rows:
            cid = row.get('id')
            sym = mapping.get(cid)
            if not sym:
                continue
            current_price = float(row.get('current_price') or 0)
            price_change_pct = float(row.get('price_change_percentage_24h') or 0)
            previous_close = current_price / (1 + price_change_pct / 100.0) if current_price > 0 and abs(price_change_pct) < 99.9 else 0
            out[sym] = {
                'shortName': row.get('name') or sym,
                'exchange': 'CRYPTO',
                'currentPrice': current_price,
                'regularMarketPrice': current_price,
                'previousClose': previous_close if previous_close > 0 else current_price,
                'open': previous_close if previous_close > 0 else current_price,
                'dayLow': float(row.get('low_24h') or 0),
                'dayHigh': float(row.get('high_24h') or 0),
                'volume': float(row.get('total_volume') or 0),
                'regularMarketVolume': float(row.get('total_volume') or 0),
                'averageVolume': float(row.get('total_volume') or 0),
                'averageVolume10days': float(row.get('total_volume') or 0),
                'marketCap': float(row.get('market_cap') or 0),
                'bid': 0,
                'ask': 0,
                'price_change_percentage_24h': price_change_pct,
                'price_change_percentage_7d': float(row.get('price_change_percentage_7d_in_currency') or 0),
                'market_cap_rank': int(row.get('market_cap_rank') or 0),
                'provider_source': 'coingecko',
                'coingecko_id': cid,
            }
        return out
    except Exception as exc:
        mark_provider_failure(str(exc))
        return {}


def fetch_coingecko_snapshot(symbol: str) -> dict:
    return fetch_coingecko_snapshots([symbol]).get(symbol, {})


def refresh_coingecko_catalog_in_background() -> None:
    def _runner():
        try:
            fetch_coingecko_catalog(True)
        except Exception:
            pass
        # After the on-disk catalog has been refreshed, bust the in-memory
        # crypto-universe cache so the *next* /stocks/results?market=crypto
        # request transparently picks up any newly-ranked coins.  Import lazily
        # to avoid a circular import at module load.
        try:
            from app.services.universe_service import bust_crypto_universe_cache
            bust_crypto_universe_cache()
        except Exception:
            pass
    try:
        threading.Thread(target=_runner, daemon=True).start()
    except Exception:
        pass
