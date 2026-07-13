"""
Input-tolerance helpers for inbound API routes.

Phase 26.18.c hardening: the API used to be strict about input shapes
(e.g. `symbol` had to be exact uppercase, `market=stocks` was the only
accepted form, booleans required exact `true`/`false`). That's fine for
machine clients but unfriendly to humans typing things by hand into the
URL bar — and harmful when phones / share-targets / browser
auto-complete munge parameter casing or whitespace.

These helpers normalize the most common loose forms so a request like

    GET /stock/  aapl  ?market=Stock&force_live=YES

is treated identically to the canonical

    GET /stock/AAPL?market=stocks&force_live=true

Symbol normalization is intentionally conservative — it strips
whitespace + URL-encoding artifacts and uppercases, but does NOT reject
characters that might legitimately appear in symbols across exchanges
(e.g. `.`, `-`, `^`). Reject decisions are deferred to the underlying
universe / cache layers, which return graceful "unknown symbol"
responses for unfamiliar tickers.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote_plus

# ---------------------------------------------------------------------------
# Symbol normalization
# ---------------------------------------------------------------------------
# Whitelist that covers all symbols we've seen across NYSE/NASDAQ/CBOE/
# Stooq/yfinance: A-Z, 0-9, '.' (BRK.B), '-' (RDS-A, ETH-USD), '^'
# (^GSPC indices), '/' (occasional preferred share notation). Anything
# else (spaces, NULs, control chars, quotes) is stripped.
_SYMBOL_OK = re.compile(r'[^A-Z0-9.\-^/]')
_WS_COLLAPSE = re.compile(r'\s+')


def normalize_symbol(raw: Any) -> str:
    """Aggressively canonicalize a user-supplied symbol string.

    Steps (in order):
      1. Coerce to str (defensively handles ints, bytes, None).
      2. URL-decode (so `%20`, `+` etc. don't survive into the value).
      3. Strip surrounding whitespace.
      4. Uppercase.
      5. Drop any character not in the symbol whitelist.

    Returns an empty string if nothing legitimate remains — callers
    should treat that as a 400-equivalent.
    """
    if raw is None:
        return ''
    if isinstance(raw, bytes):
        try:
            raw = raw.decode('utf-8', errors='replace')
        except Exception:  # noqa: BLE001
            return ''
    s = str(raw)
    # URL-decode and collapse whitespace defensively even if FastAPI already
    # decoded the value — defense in depth costs nothing here.
    try:
        s = unquote_plus(s)
    except Exception:  # noqa: BLE001
        pass
    s = s.strip().upper()
    if not s:
        return ''
    # Replace any character not in the whitelist with empty (NOT space —
    # we don't want "AAP L" to become "AAP L" which we'd then re-split).
    s = _SYMBOL_OK.sub('', s)
    return s


# ---------------------------------------------------------------------------
# Market normalization
# ---------------------------------------------------------------------------
_MARKET_ALIASES = {
    'stocks': 'stocks', 'stock': 'stocks', 'equity': 'stocks',
    'equities': 'stocks', 'stk': 'stocks', 'us': 'stocks',
    'crypto': 'crypto', 'cryptos': 'crypto', 'cryptocurrency': 'crypto',
    'cryptocurrencies': 'crypto', 'coin': 'crypto', 'coins': 'crypto',
}


def normalize_market(raw: Any, default: str = 'stocks') -> str:
    """Coerce a user-supplied market hint to one of {'stocks','crypto'}.

    Unknown values fall back to `default`. Case + whitespace insensitive.
    """
    if not raw:
        return default
    s = str(raw).strip().lower()
    return _MARKET_ALIASES.get(s, default)


# ---------------------------------------------------------------------------
# Loose boolean parser
# ---------------------------------------------------------------------------
_TRUE_TOKENS = frozenset({'true', '1', 'yes', 'y', 'on', 'enable', 'enabled'})
_FALSE_TOKENS = frozenset({'false', '0', 'no', 'n', 'off', 'disable', 'disabled', ''})


def loose_bool(raw: Any, default: bool = False) -> bool:
    """Accept any of the common loose-boolean conventions.

      truthy: True, 1, "true", "yes", "y", "on", "enable", "enabled"
      falsy : False, 0, "false", "no", "n", "off", "disable", "disabled", ""

    Anything else falls back to `default`.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return bool(raw)
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if s in _TRUE_TOKENS:
        return True
    if s in _FALSE_TOKENS:
        return False
    return default


# ---------------------------------------------------------------------------
# Bounded int coercer
# ---------------------------------------------------------------------------
def loose_int(raw: Any, default: int = 0, *,
              lo: int | None = None, hi: int | None = None) -> int:
    """Coerce to int with bounds-clamping. Accepts strings, floats,
    and even strings with stray whitespace / leading '+'. Returns
    `default` on any conversion failure.
    """
    if raw is None or raw == '':
        return default
    try:
        if isinstance(raw, bool):
            v = int(raw)
        elif isinstance(raw, (int, float)):
            v = int(raw)
        else:
            v = int(str(raw).strip().replace(',', ''))
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


# ---------------------------------------------------------------------------
# Search-query normalizer
# ---------------------------------------------------------------------------
def normalize_search_query(raw: Any, *, max_len: int = 64) -> str:
    """Trim, collapse internal whitespace, cap length. Always returns a
    safe string for downstream regex/SQL use (we don't strip dangerous
    characters here — callers must still parameterize their queries).
    """
    if not raw:
        return ''
    s = str(raw)
    try:
        s = unquote_plus(s)
    except Exception:  # noqa: BLE001
        pass
    s = _WS_COLLAPSE.sub(' ', s.strip())
    if len(s) > max_len:
        s = s[:max_len]
    return s
