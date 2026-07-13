"""
Regulatory-signal-to-scanner bridge.

Per user spec (Phase 26 Option 1A):
  - A large insider BUY of a company's securities should positively impact
    that ticker's ranking; a sell should impact it negatively.
  - Role-based weighting: CEO / CFO / Chair carry full weight; COO / President /
    CTO / CIO / General Counsel get 0.85x; other officers 0.6x; directors only
    0.4x; 10% holders / SC 13D-G 0.5x; "is_other" only 0.25x.
  - Confirming-direction clustering within a 7-day window:
       >=3 same-direction events => +10% on the aggregate impact
       >=5 same-direction events => +25% on the aggregate impact
    (only the higher tier applies)
  - Staleness curve: full weight for age 0-7 days, then linear ramp to 0 over
    7-15 days, then ignored. (legacy 5/3 settings are upgraded in-place.)

Output shape (per symbol):
    {
        'symbol': 'AAPL',
        'score_delta': -8.0..+8.0,      # bounded +- points applied to composite
        'weight': 0.0..1.0,             # how confident we are in this delta
        'reason': 'short human string', # one-liner for the UI
        'staleness_days': 1.2,
        'cluster_boost': 0.0|0.10|0.25, # which clustering tier triggered (if any)
        'top_role_weight': 0.0..1.0,    # max role weight observed (for UI)
        'raw_events': [...],            # the supporting filings (for detail panel)
    }

The service maintains an in-process index keyed by ticker; the index is rebuilt
from SQLite either on a TTL or on explicit /api/regulatory/signal-refresh.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from app.regulatory.db.database import DB_PATH
from app.regulatory.services.storage_service import get_settings

log = logging.getLogger('app.regulatory.signal')

# In-process cache so the main scanner can call get_signal_for_symbol thousands
# of times per minute without hammering SQLite.
_INDEX: dict[str, dict[str, Any]] = {}
_INDEX_BUILT_AT: float = 0.0
_INDEX_TTL_SECONDS: float = 60.0
_index_lock = asyncio.Lock()


# Transaction-type mapping -> directional sign
# Per app.regulatory.services.sec_service.TXN_MAP
_BULLISH = {'open_market_buy'}
_BEARISH = {'open_market_sell', 'tax_withholding_or_payment', 'gift'}
# Neutral-ish: grant/award (A), derivative_exercise (M), disposition_to_issuer (D),
# equity_swap (K), other (J), early_report (V). These get tiny weight or zero.

# ---------------------------------------------------------------------------
# Role weighting (Phase 26 Option 1A) - precedence: highest matching role wins.
# Title heuristics are case-insensitive substring matches; "general counsel"
# must match before "counsel" to avoid mis-classification.
# ---------------------------------------------------------------------------
_TIER1_PATTERNS = (
    r'\bceo\b', r'chief executive', r'\bcfo\b', r'chief financial',
    r'\bchair(man|woman|person)?\b', r'\bchair of the board\b',
)
_TIER2_PATTERNS = (
    r'\bcoo\b', r'chief operating',
    # "President" alone counts as Tier 2 (CEO-equivalent), but "Vice
    # President" / "Senior Vice President" / "Executive Vice President"
    # must fall through to Tier 3. Negative lookbehind enforces that.
    r'(?<!vice )(?<!senior vice )(?<!executive vice )(?<!\bvp )(?<!\bsvp )(?<!\bevp )\bpresident\b',
    r'\bcto\b', r'chief technology',
    r'\bcio\b', r'chief information', r'chief investment',
    r'general counsel', r'chief legal',
    r'\bcso\b', r'chief security', r'chief strategy',
    r'\bcmo\b', r'chief marketing',
    r'\bcpo\b', r'chief product', r'chief people',
    r'\bcro\b', r'chief revenue', r'chief risk',
    r'\bcco\b', r'chief compliance', r'chief commercial',
)
_TIER3_PATTERNS = (
    r'\bvice president\b', r'\bv\.?p\.?\b',
    r'\bsvp\b', r'\bevp\b', r'senior vice president', r'executive vice president',
    r'\bofficer\b',  # generic "officer" titles
    r'\bdirector\b.*(financ|technolog|operations|strategy|engineering)',
)
_TIER1_RE = re.compile('|'.join(_TIER1_PATTERNS), re.IGNORECASE)
_TIER2_RE = re.compile('|'.join(_TIER2_PATTERNS), re.IGNORECASE)
_TIER3_RE = re.compile('|'.join(_TIER3_PATTERNS), re.IGNORECASE)


def _role_weight(evt: dict) -> float:
    """Pick the strongest role weight applicable to this filing event.

    Precedence (highest wins, NOT additive):
      Tier 1 CEO/CFO/Chair         -> 1.00
      Tier 2 COO/Pres/CTO/CIO/GC   -> 0.85
      Tier 3 Other officers (VP..) -> 0.60
      Directors-only               -> 0.40
      10% holders / 13D-G          -> 0.50
      is_other only                -> 0.25
      Fallback                     -> 0.40 (treat unknown as director-equivalent)
    """
    title = (evt.get('officer_title') or '').strip()
    is_officer = bool(evt.get('is_officer'))
    is_director = bool(evt.get('is_director'))
    is_ten_pct = bool(evt.get('is_ten_percent_owner'))
    is_other_flag = bool(evt.get('is_other'))
    form = (evt.get('form') or '').upper()
    txn_type = (evt.get('txn_type') or '').lower()

    # 13D/G beneficial ownership reports use the 10%-holder lane regardless of
    # other flags; they are not company insiders in the classic sense.
    if form.startswith('SC 13D') or form.startswith('SC 13G') \
            or txn_type == 'beneficial_ownership_report':
        return 0.50

    if is_officer and title:
        if _TIER1_RE.search(title):
            return 1.00
        if _TIER2_RE.search(title):
            return 0.85
        if _TIER3_RE.search(title):
            return 0.60
        # Officer with unmapped title -> moderate weight
        return 0.55

    if is_officer and not title:
        # Officer flag without a title is still notable; treat as "other officer".
        return 0.55

    if is_ten_pct:
        return 0.50

    if is_director and not is_officer:
        return 0.40

    if is_other_flag and not (is_officer or is_director or is_ten_pct):
        return 0.25

    # Fallback (no flags set) - treat as director-equivalent rather than zero,
    # because the SEC ownership form was still filed.
    return 0.40


def _parse_filing_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # SEC filing dates are YYYY-MM-DD; tolerate ISO too.
        if 'T' in s:
            return datetime.fromisoformat(s.replace('Z', '+00:00'))
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _staleness_days(filing_date: datetime, now: datetime) -> float:
    return max(0.0, (now - filing_date).total_seconds() / 86400.0)


def _dollar_value(shares: float | None, price: float | None) -> float:
    if not shares or not price:
        return 0.0
    return abs(float(shares) * float(price))


def _weight_from_value(value: float, min_v: float, strong_v: float) -> float:
    """Smooth ramp from 0 (at min_v) to 1 (at strong_v)."""
    if value <= min_v:
        return 0.0
    if value >= strong_v:
        return 1.0
    return (value - min_v) / max(1.0, (strong_v - min_v))


def _staleness_factor(age_days: float, max_age_days: float, decay_days: float) -> float:
    """Linear decay: full weight up to (max_age_days - decay_days), then linearly
    decays to ~0 at max_age_days, then 0 beyond that (signal disregarded).

    With Phase 26 defaults (max=15, decay=8): full weight 0-7 days, ramps to 0
    over days 7..15, ignored beyond 15 days.
    """
    if age_days >= max_age_days:
        return 0.0
    full_until = max(0.0, max_age_days - decay_days)
    if age_days <= full_until:
        return 1.0
    # Linear ramp down from full_until -> max_age_days
    span = max(0.001, max_age_days - full_until)
    return max(0.0, 1.0 - (age_days - full_until) / span)


async def _read_recent_filings(max_age_days: int) -> list[dict]:
    """Pull all filings with a non-null issuer_ticker that are within the
    staleness window. We pull role/title fields too so we can apply C-suite
    weighting downstream.

    Phase 26.15.b: previously over-fetched and then Python-filtered with a
    hard 7500-row cap. For heavy users running autoscan for weeks the table
    grows past 20k+ rows in the lookback window, so the cap was silently
    dropping older filings - boost-eligible symbols would disappear from
    the rebuilt index. Fix: filter by `filing_date >= cutoff` directly in
    SQL so the row count scales naturally with the actual relevant window,
    and bump the hard ceiling to a much higher safety value.
    """
    if not DB_PATH.exists():
        return []
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=max_age_days + 1)).date().isoformat()
    hard_cap = max(50_000, max_age_days * 5000)  # 75k @ 15d default
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            '''SELECT issuer_ticker, issuer_cik, issuer_name, reporting_owner_name,
                      filing_date, transaction_code, transaction_type, shares,
                      price_per_share, percent_owned, form, accession_number,
                      is_director, is_officer, is_ten_percent_owner, is_other,
                      officer_title
               FROM filings
               WHERE issuer_ticker IS NOT NULL AND issuer_ticker != ''
                 AND filing_date IS NOT NULL
                 AND filing_date >= ?
               ORDER BY filing_date DESC, id DESC
               LIMIT ?''',
            (cutoff_date, hard_cap),
        )
        rows = await cur.fetchall()
    out: list[dict] = []
    now = datetime.now(timezone.utc)
    for r in rows:
        fd = _parse_filing_date(r['filing_date'])
        if not fd:
            continue
        if _staleness_days(fd, now) > max_age_days:
            continue
        out.append({
            'symbol': (r['issuer_ticker'] or '').upper(),
            'issuer_cik': r['issuer_cik'],
            'issuer_name': r['issuer_name'],
            'owner_name': r['reporting_owner_name'],
            'filing_date': fd,
            'txn_code': r['transaction_code'],
            'txn_type': r['transaction_type'],
            'shares': float(r['shares']) if r['shares'] else 0.0,
            'price': float(r['price_per_share']) if r['price_per_share'] else 0.0,
            'percent_owned': float(r['percent_owned']) if r['percent_owned'] else 0.0,
            'form': r['form'],
            'accession': r['accession_number'],
            'is_director': bool(r['is_director']),
            'is_officer': bool(r['is_officer']),
            'is_ten_percent_owner': bool(r['is_ten_percent_owner']),
            'is_other': bool(r['is_other']),
            'officer_title': r['officer_title'],
        })
    return out


def _classify_event(evt: dict, cfg: dict) -> dict:
    """Return per-event {direction, contribution, value, weight} given config.

    Phase 26: multiplies the event weight by a role-based multiplier (1.0 down
    to 0.25 depending on title / officer / director / 10%-holder flags).
    """
    txn = (evt.get('txn_type') or '').lower()
    sign = 0
    if txn in _BULLISH:
        sign = +1
    elif txn in _BEARISH:
        sign = -1
    # Beneficial ownership reports (13D/G) - interpret high % as a long-bias signal
    if evt.get('percent_owned', 0) >= cfg['ownership_threshold_percent']:
        # Big ownership stake reported = mildly bullish
        sign = max(sign, +1)
    value = _dollar_value(evt.get('shares', 0), evt.get('price', 0))
    if value <= 0 and evt.get('percent_owned', 0) > 0:
        # If no $ value but a meaningful ownership %, treat as if it crossed
        # the strong-value threshold (these are big block holders by definition).
        value = cfg['signal_strong_dollar_value']
    weight_v = _weight_from_value(
        value,
        cfg['signal_min_dollar_value'],
        cfg['signal_strong_dollar_value'],
    )
    now = datetime.now(timezone.utc)
    age_days = _staleness_days(evt['filing_date'], now)
    weight_t = _staleness_factor(age_days, cfg['signal_max_age_days'], cfg['signal_decay_days'])
    role_w = _role_weight(evt)
    combined_weight = weight_v * weight_t * role_w
    contribution = sign * combined_weight  # bounded magnitude ~[0, 1]
    return {
        'sign': sign,
        'weight': combined_weight,
        'role_weight': role_w,
        'staleness_weight': weight_t,
        'value_weight': weight_v,
        'contribution': contribution,
        'value': value,
        'age_days': age_days,
        'txn_type': txn,
        'owner': evt.get('owner_name'),
        'officer_title': evt.get('officer_title'),
        'is_director': evt.get('is_director'),
        'is_officer': evt.get('is_officer'),
        'is_ten_percent_owner': evt.get('is_ten_percent_owner'),
        'shares': evt.get('shares'),
        'price': evt.get('price'),
        'percent_owned': evt.get('percent_owned'),
        'filing_date': evt['filing_date'].isoformat(),
        'form': evt.get('form'),
        'accession': evt.get('accession'),
    }


async def _read_recent_awards(max_age_days: int) -> list[dict]:
    """Pull all award rows within the staleness window. We join via the
    tracked_companies table to resolve recipient_name -> ticker. Any award whose
    recipient name fuzzy-matches a tracked company's issuer_name is attributed
    to that company's ticker.

    Phase 26.15.b: filter `action_date >= cutoff` in SQL so the 5000-row
    cap doesn't silently truncate older awards relevant to symbols whose
    boost depended on them.
    """
    if not DB_PATH.exists():
        return []
    now = datetime.now(timezone.utc)
    cutoff_date = (now - timedelta(days=max_age_days + 1)).date().isoformat()
    hard_cap = max(20_000, max_age_days * 2000)
    out: list[dict] = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Load tracked companies for name resolution.
        cur = await db.execute(
            'SELECT cik, issuer_name, issuer_ticker FROM tracked_companies WHERE issuer_ticker IS NOT NULL AND issuer_ticker != ""'
        )
        tracked = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            '''SELECT generated_internal_id, award_id, recipient_name, awarding_agency,
                      awarding_subagency, action_date, amount, description
               FROM awards
               WHERE action_date IS NOT NULL
                 AND action_date >= ?
               ORDER BY action_date DESC, id DESC
               LIMIT ?''',
            (cutoff_date, hard_cap),
        )
        rows = await cur.fetchall()
    # Use the ticker->CIK service's name normalization for matching.
    from app.regulatory.services.cik_lookup_service import (
        _normalize_name as norm,
        ticker_for_recipient_name,
    )
    # Pre-index tracked-company normalized name -> ticker
    tracked_idx: dict[str, str] = {}
    for tc in tracked:
        nm = norm(tc.get('issuer_name') or '')
        tk = (tc.get('issuer_ticker') or '').upper()
        if nm and tk:
            tracked_idx[nm] = tk
    for r in rows:
        action_date = r['action_date']
        try:
            if action_date and 'T' in action_date:
                ad = datetime.fromisoformat(action_date.replace('Z', '+00:00'))
            elif action_date:
                ad = datetime.fromisoformat(action_date).replace(tzinfo=timezone.utc)
            else:
                continue
        except Exception:
            continue
        age = _staleness_days(ad, now)
        if age > max_age_days:
            continue
        # Resolve to a ticker - first try tracked-company exact normalized match,
        # then fall back to the global ticker->CIK reverse lookup.
        rec_name = r['recipient_name'] or ''
        rec_norm = norm(rec_name)
        ticker = tracked_idx.get(rec_norm)
        if not ticker:
            ticker = ticker_for_recipient_name(rec_name)
        if not ticker:
            continue  # no confident match -> skip; we'd just be guessing
        out.append({
            'symbol': ticker,
            'recipient_name': rec_name,
            'amount': float(r['amount']) if r['amount'] else 0.0,
            'action_date': ad,
            'age_days': age,
            'awarding_agency': r['awarding_agency'],
            'description': r['description'],
            'award_id': r['award_id'] or r['generated_internal_id'],
        })
    return out


def _classify_award(evt: dict, cfg: dict) -> dict:
    """Awards always have a positive sign (winning a federal contract is a
    revenue tailwind). Weight scales with $ amount; the staleness factor uses
    the same curve as insider events. Awards are treated as carrying a role
    weight of 1.0 because they represent the company itself, not an individual.
    """
    amount = float(evt.get('amount') or 0.0)
    weight_v = _weight_from_value(
        amount,
        cfg['signal_min_dollar_value'],
        cfg['signal_strong_dollar_value'],
    )
    weight_t = _staleness_factor(evt['age_days'], cfg['signal_max_age_days'], cfg['signal_decay_days'])
    combined_weight = weight_v * weight_t
    contribution = +1 * combined_weight
    return {
        'sign': +1,
        'weight': combined_weight,
        'role_weight': 1.0,
        'staleness_weight': weight_t,
        'value_weight': weight_v,
        'contribution': contribution,
        'value': amount,
        'age_days': evt['age_days'],
        'recipient_name': evt.get('recipient_name'),
        'awarding_agency': evt.get('awarding_agency'),
        'description': evt.get('description'),
        'award_id': evt.get('award_id'),
        'action_date': evt['action_date'].isoformat(),
        'kind': 'contract_award',
    }


# ---------------------------------------------------------------------------
# Phase 26: 7-day clustering bonus on confirming-direction events.
# ---------------------------------------------------------------------------
_CLUSTER_WINDOW_DAYS = 7.0
_CLUSTER_TIER_BONUSES = (
    # threshold, multiplier
    (5, 0.25),
    (3, 0.10),
)


def _cluster_multiplier(classified: list[dict]) -> tuple[float, int, int]:
    """Compute the clustering multiplier on the aggregate impact.

    Counts the number of MATERIAL same-direction events whose age is within the
    7-day clustering window. "Material" = weight > 0 (i.e. event survived the
    staleness/value/role gates).

    Returns: (multiplier, bull_count, bear_count)
        multiplier == 1.00 if no tier triggers
        multiplier == 1.10 for the >=3 tier
        multiplier == 1.25 for the >=5 tier (overrides the >=3 tier)
    """
    bull = sum(
        1 for c in classified
        if c.get('weight', 0) > 0
        and c.get('sign', 0) > 0
        and c.get('age_days', 99) <= _CLUSTER_WINDOW_DAYS
    )
    bear = sum(
        1 for c in classified
        if c.get('weight', 0) > 0
        and c.get('sign', 0) < 0
        and c.get('age_days', 99) <= _CLUSTER_WINDOW_DAYS
    )
    dominant_count = max(bull, bear)
    multiplier = 1.0
    for threshold, bonus in _CLUSTER_TIER_BONUSES:
        if dominant_count >= threshold:
            multiplier = 1.0 + bonus
            break  # _CLUSTER_TIER_BONUSES is ordered highest-first
    return multiplier, bull, bear


def _aggregate_per_symbol(events_by_symbol: dict[str, list[dict]],
                          awards_by_symbol: dict[str, list[dict]],
                          cfg: dict) -> dict[str, dict]:
    """Roll up classified events into one signal per symbol. Combines insider
    filings AND contract awards; either source alone is enough to register.

    Phase 26 additions:
      - Applies 7-day clustering multiplier on the aggregate before tanh squash.
      - Records the role-weight context for downstream UI surfaces.
    """
    out: dict[str, dict] = {}
    max_boost = cfg['signal_max_boost']
    all_symbols = set(events_by_symbol) | set(awards_by_symbol)
    for sym in all_symbols:
        events = events_by_symbol.get(sym, [])
        awards = awards_by_symbol.get(sym, [])
        classified_filings = [_classify_event(e, cfg) for e in events]
        classified_awards = [_classify_award(a, cfg) for a in awards]
        classified = classified_filings + classified_awards

        # 7-day clustering bonus (Phase 26.1b).
        cluster_mult, bull_cluster, bear_cluster = _cluster_multiplier(classified)
        cluster_bonus = cluster_mult - 1.0  # 0.0, 0.10, or 0.25

        # Sum of signed contributions; clustering amplifies before tanh squash
        # so multiple confirming events compound but stay bounded.
        total = sum(c['contribution'] for c in classified) * cluster_mult
        squashed = math.tanh(total / 2.0)
        score_delta = round(squashed * max_boost, 2)

        weights = [c['weight'] for c in classified if c['weight'] > 0]
        confidence = min(1.0, sum(weights) / 3.0) if weights else 0.0
        # Clustering also boosts confidence slightly so the UI shows the user
        # the cluster signal is being trusted more.
        if cluster_bonus > 0:
            confidence = min(1.0, confidence * (1.0 + cluster_bonus * 0.5))

        # Track the strongest role observed (drives the UI badge tier).
        top_role_weight = max((c.get('role_weight') or 0.0) for c in classified) if classified else 0.0

        classified.sort(key=lambda c: c['age_days'])  # freshest first
        # NOTE: Phase 26 retires the legacy "halve if no fresh confirming"
        # rule because clustering already enforces this dynamic.

        # Phase 26.13: aggregate notional across all confirming events in
        # the cluster window. A single Form 4 often splits one purchase
        # across many price/lot rows, so the "freshest event" pick
        # underrepresents the operator's true conviction. We surface the
        # cluster-window total in the reason text AND in the snapshot
        # payload so the dashboard reflects the full dollar exposure.
        cluster_window_events = [
            c for c in classified
            if c.get('age_days', 99) <= _CLUSTER_WINDOW_DAYS
            and (c.get('value') or 0) > 0
        ]
        aggregate_notional = sum(c.get('value') or 0 for c in cluster_window_events)
        # Also compute signed aggregate (positive for net-bullish events,
        # negative for net-bearish) so the UI can show direction at a glance.
        signed_aggregate_notional = sum(
            (c.get('value') or 0) * (1 if (c.get('sign') or 0) >= 0 else -1)
            for c in cluster_window_events
        )

        # Build a human-readable reason - prefer the freshest "signal", then
        # append a tag if there's also activity in the other channel.
        cluster_tag = ''
        if cluster_bonus > 0:
            tier_label = f"+{int(round(cluster_bonus * 100))}%"
            cluster_tag = (f" \u00b7 cluster {tier_label}: "
                           f"{max(bull_cluster, bear_cluster)} confirming events in 7d")
        if abs(score_delta) < 0.1:
            reason = f"{len(classified)} stale/minor events (no material signal)"
        else:
            # Phase 26.13: prefer the freshest event with an actual non-zero
            # dollar value. The pre-fix logic just used `classified[0]`,
            # which often pointed at a phantom "holding" row (value=0)
            # produced by the now-fixed Form 4 parser. We keep that as a
            # fallback for the rare case where every cluster-window event
            # genuinely is value-less (e.g. director appointment notice).
            top = next(
                (c for c in classified if (c.get('value') or 0) > 0),
                classified[0],
            )
            if top.get('kind') == 'contract_award':
                amt_m = (top.get('value') or 0) / 1e6
                reason = (f"federal contract win {top.get('awarding_agency') or 'agency'} "
                          f"(${amt_m:.2f}M, {top['age_days']:.1f}d ago)")
            else:
                direction = ('insider buy' if top['sign'] > 0
                             else 'insider sell' if top['sign'] < 0
                             else 'insider activity')
                title = top.get('officer_title') or ''
                role_tag = ''
                rw = top.get('role_weight') or 0.0
                if rw >= 0.99:
                    role_tag = ' (C-suite)'
                elif rw >= 0.84:
                    role_tag = ' (senior exec)'
                elif rw >= 0.59 and top.get('is_officer'):
                    role_tag = ' (officer)'
                elif top.get('is_ten_percent_owner') and not top.get('is_officer'):
                    role_tag = ' (10% holder)'
                elif top.get('is_director') and not top.get('is_officer'):
                    role_tag = ' (director)'
                owner = (top.get('owner') or 'reporting person')
                owner_disp = owner + (f', {title}' if title and role_tag in ('', ' (officer)') else '')
                # Surface aggregate cluster-window notional when there is
                # one (>1 confirming event with value), otherwise show the
                # top event's notional alone.
                top_value_m = (top.get('value') or 0) / 1e6
                if len(cluster_window_events) > 1 and aggregate_notional > 0:
                    agg_m = aggregate_notional / 1e6
                    reason = (
                        f"{direction} by {owner_disp}{role_tag} "
                        f"({top['age_days']:.1f}d ago, "
                        f"${top_value_m:.2f}M most-recent \u00b7 "
                        f"${agg_m:.2f}M total across {len(cluster_window_events)} events)"
                    )
                else:
                    reason = (
                        f"{direction} by {owner_disp}{role_tag} "
                        f"({top['age_days']:.1f}d ago, ${top_value_m:.2f}M notional)"
                    )
            # If both channels contributed, surface that.
            has_both = bool(classified_filings) and bool(classified_awards)
            if has_both:
                reason += f" \u00b7 +{len(classified_awards)} contract event(s)" if top.get('kind') != 'contract_award' \
                    else f" \u00b7 +{len(classified_filings)} insider event(s)"
            reason += cluster_tag

        out[sym] = {
            'symbol': sym,
            'score_delta': round(score_delta, 2),
            'weight': round(confidence, 3),
            'reason': reason,
            'staleness_days': round(classified[0]['age_days'], 2) if classified else None,
            'event_count': len(classified),
            'insider_event_count': len(classified_filings),
            'award_event_count': len(classified_awards),
            'bull_cluster_count': bull_cluster,
            'bear_cluster_count': bear_cluster,
            'cluster_bonus': round(cluster_bonus, 3),  # 0.00 / 0.10 / 0.25
            'top_role_weight': round(top_role_weight, 3),
            # Phase 26.13: cluster-window aggregate notional so the UI can
            # show the FULL dollar exposure, not just the freshest row.
            'aggregate_notional': round(aggregate_notional, 2),
            'signed_aggregate_notional': round(signed_aggregate_notional, 2),
            'cluster_event_count': len(cluster_window_events),
            'raw_events': classified[:10],  # cap to keep payload light
        }
    return out


async def _load_config() -> dict:
    s = await get_settings()

    def f(k, default):
        try:
            return float(s.get(k, default))
        except Exception:
            return float(default)
    # Phase 26 defaults: 15-day total window with full strength for 0-7 days.
    return {
        'signal_max_age_days': f('signal_max_age_days', 15),
        'signal_decay_days': f('signal_decay_days', 8),
        'signal_max_boost': f('signal_max_boost', 8.0),
        'signal_min_dollar_value': f('signal_min_dollar_value', 25000),
        'signal_strong_dollar_value': f('signal_strong_dollar_value', 1_000_000),
        'ownership_threshold_percent': f('ownership_threshold_percent', 5),
    }


async def _rebuild_index() -> int:
    global _INDEX, _INDEX_BUILT_AT
    cfg = await _load_config()
    filing_rows = await _read_recent_filings(int(cfg['signal_max_age_days']))
    award_rows = await _read_recent_awards(int(cfg['signal_max_age_days']))
    grouped_filings: dict[str, list[dict]] = defaultdict(list)
    for r in filing_rows:
        if r['symbol']:
            grouped_filings[r['symbol']].append(r)
    grouped_awards: dict[str, list[dict]] = defaultdict(list)
    for r in award_rows:
        if r['symbol']:
            grouped_awards[r['symbol']].append(r)
    _INDEX = _aggregate_per_symbol(grouped_filings, grouped_awards, cfg)
    _INDEX_BUILT_AT = time.monotonic()
    # Phase 26.22: when the index has zero symbols (autoscan hasn't
    # produced any insider/award rows yet) this rebuild log is just
    # noise — once a minute, forever. Demote to DEBUG in that case.
    # When the index actually has data, INFO surfaces the value.
    if len(_INDEX) > 0:
        log.info('regulatory signal index rebuilt: %d symbols with non-zero data', len(_INDEX))
    else:
        log.debug('regulatory signal index rebuilt: 0 symbols (no autoscan data yet)')
    return len(_INDEX)


async def _maybe_rebuild():
    global _INDEX_BUILT_AT
    async with _index_lock:
        if time.monotonic() - _INDEX_BUILT_AT > _INDEX_TTL_SECONDS:
            await _rebuild_index()


# ---------------------------------------------------------------------------
# Public sync API - designed to be called from sync scoring code paths.
# We expose a synchronous shim that just reads from the in-process index.
# ---------------------------------------------------------------------------

def get_signal_sync(symbol: str) -> dict:
    """Synchronous, blocking-free lookup. Returns zero-signal stub if absent.

    The index is rebuilt asynchronously elsewhere - this function never blocks.
    """
    if not symbol:
        return _zero_signal('')
    sig = _INDEX.get(symbol.upper())
    if not sig:
        return _zero_signal(symbol)
    return sig


# ---------------------------------------------------------------------------
# Phase 26.16 / Tier 2.2 — batch-friendly index snapshot API
# ---------------------------------------------------------------------------
# `get_signal_sync` is already lock-free (just a dict.get on a module-level
# global), but in tight scoring loops the global lookup forces the GIL to
# bounce between worker threads and is unfriendly to CPU caches. The
# helpers below let a batch of scorers grab a single reference to the
# current `_INDEX` and reuse it for every row, eliminating both effects.
#
# Atomicity: `refresh_signal_index()` replaces `_INDEX` via `global _INDEX
# = ...` — name-rebinding is atomic in CPython, so the snapshot a batch
# captures is guaranteed to be internally consistent for the lifetime of
# the batch (it's a frozen view of "the index as of when the batch
# started"; later rebuilds don't affect the snapshot).

def get_signal_index_snapshot() -> dict:
    """Return a reference to the currently-published regulatory signal
    index. Callers MUST treat the returned dict as read-only — the same
    instance is shared across the process.

    Cheap (O(1) — no copy). Pair with `get_signal_sync_from()` to look
    up symbols without re-acquiring the global on every call.
    """
    return _INDEX


def get_signal_sync_from(index: dict, symbol: str) -> dict:
    """Identical semantics to `get_signal_sync` but reads from a caller-
    supplied snapshot rather than the module global.

    Use inside batch loops: snapshot once before the loop, then look up
    every symbol against the snapshot. This (1) avoids cross-thread GIL
    bounces on the module-level read, and (2) gives the batch a
    consistent view of the index even if a background refresh lands
    mid-batch.
    """
    if not symbol:
        return _zero_signal('')
    sig = index.get(symbol.upper()) if isinstance(index, dict) else None
    if not sig:
        return _zero_signal(symbol)
    return sig


def _zero_signal(symbol: str) -> dict:
    return {
        'symbol': (symbol or '').upper(),
        'score_delta': 0.0,
        'weight': 0.0,
        'reason': 'no recent insider/ownership signal',
        'staleness_days': None,
        'event_count': 0,
        'insider_event_count': 0,
        'award_event_count': 0,
        'bull_cluster_count': 0,
        'bear_cluster_count': 0,
        'cluster_bonus': 0.0,
        'top_role_weight': 0.0,
        'raw_events': [],
    }


# ---------------------------------------------------------------------------
# Async API for the routes
# ---------------------------------------------------------------------------

async def get_signal_for_symbol(symbol: str) -> dict:
    await _maybe_rebuild()
    return get_signal_sync(symbol)


async def get_signal_summary(limit: int = 100) -> dict:
    await _maybe_rebuild()
    items = sorted(
        _INDEX.values(),
        key=lambda s: (abs(s.get('score_delta', 0)), s.get('weight', 0)),
        reverse=True,
    )
    return {
        'count': len(items),
        'built_at_monotonic': _INDEX_BUILT_AT,
        'top': items[:limit],
    }


async def refresh_signal_index() -> int:
    async with _index_lock:
        return await _rebuild_index()


async def get_auto_results_list(limit: int = 200) -> dict:
    """Auto-populated sorted result list for the regulatory subpage.

    Returns every ticker with at least one insider event OR contract award
    within the staleness window, sorted by:
      1. Has-fresh-confirming-activity first (age <= decay window)
      2. Absolute score impact (|score_delta|)
      3. Confidence weight

    Each row carries everything the UI needs to render a single line:
      {symbol, score_delta, weight, reason, staleness_days,
       insider_event_count, award_event_count, freshest_filing, freshest_award}
    """
    await _maybe_rebuild()
    cfg = await _load_config()
    # "Fresh" = within the full-weight portion of the staleness curve.
    fresh_window = max(0.0, cfg['signal_max_age_days'] - cfg['signal_decay_days'])

    rows: list[dict] = []
    for sig in _INDEX.values():
        if sig.get('event_count', 0) <= 0:
            continue
        # Pull the freshest event of each kind so the UI can show both pills.
        freshest_filing = None
        freshest_award = None
        for evt in (sig.get('raw_events') or []):
            if evt.get('kind') == 'contract_award':
                if freshest_award is None or evt['age_days'] < freshest_award['age_days']:
                    freshest_award = evt
            else:
                if freshest_filing is None or evt['age_days'] < freshest_filing['age_days']:
                    freshest_filing = evt
        rows.append({
            **sig,
            'is_fresh': bool(sig.get('staleness_days') is not None
                             and sig['staleness_days'] <= fresh_window),
            'freshest_filing': freshest_filing,
            'freshest_award': freshest_award,
        })

    rows.sort(key=lambda r: (
        not r.get('is_fresh'),                 # fresh first
        -abs(r.get('score_delta', 0) or 0),
        -float(r.get('weight', 0) or 0),
    ))
    return {
        'count': len(rows),
        'cutoff_days': cfg['signal_max_age_days'],
        'decay_days': cfg['signal_decay_days'],
        'fresh_window_days': fresh_window,
        'top': rows[:limit],
        'built_at_monotonic': _INDEX_BUILT_AT,
    }
