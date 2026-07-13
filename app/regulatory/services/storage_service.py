import hashlib
from contextlib import asynccontextmanager

import aiosqlite

from app.regulatory.db.database import DB_PATH
from app.regulatory.models.schemas import FilingEvent, AwardEvent


# Phase 26.8: every storage call goes through this wrapper so we get a
# consistent 10-second busy_timeout + WAL journal across all 17 call sites.
# Windows AV scanners and the maintenance loop's VACUUM occasionally hold
# the DB file long enough to trip "database is locked" without a generous
# busy_timeout; the WAL journal also lets reads and writes overlap.
@asynccontextmanager
async def _connect():
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        try:
            await db.execute('PRAGMA busy_timeout = 10000')
            await db.execute('PRAGMA journal_mode = WAL')
            await db.execute('PRAGMA synchronous = NORMAL')
        except aiosqlite.Error:
            # PRAGMAs are best-effort; never crash a write on PRAGMA failure.
            pass
        yield db


def filing_unique_key(item: FilingEvent) -> str:
    raw = '|'.join([
        item.accession_number or '', item.form or '', item.reporting_owner_name or '',
        item.transaction_code or '', str(item.shares or ''), str(item.price_per_share or ''),
        str(item.percent_owned or '')
    ])
    return hashlib.sha256(raw.encode()).hexdigest()

async def save_watchlist(cik: str, recipient: str):
    async with _connect() as db:
        await db.execute('INSERT INTO watchlists (cik, recipient) VALUES (?, ?)', (cik, recipient))
        await db.commit()

async def list_watchlists():
    async with _connect() as db:
        cur = await db.execute('SELECT id, cik, recipient, created_at FROM watchlists ORDER BY id DESC')
        rows = await cur.fetchall()
        return [dict(id=r[0], cik=r[1], recipient=r[2], created_at=r[3]) for r in rows]

async def upsert_tracked_company(cik: str, issuer_name: str = None, issuer_ticker: str = None, filing_date: str = None, source: str = 'insider_discovery'):
    async with _connect() as db:
        await db.execute('''
            INSERT INTO tracked_companies (cik, issuer_name, issuer_ticker, source, last_seen_filing_date, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(cik) DO UPDATE SET
                issuer_name=COALESCE(excluded.issuer_name, tracked_companies.issuer_name),
                issuer_ticker=COALESCE(excluded.issuer_ticker, tracked_companies.issuer_ticker),
                last_seen_filing_date=COALESCE(excluded.last_seen_filing_date, tracked_companies.last_seen_filing_date),
                source=COALESCE(excluded.source, tracked_companies.source),
                updated_at=CURRENT_TIMESTAMP
        ''', (cik, issuer_name, issuer_ticker, source, filing_date))
        await db.commit()

async def list_tracked_companies(limit: int = 200):
    async with _connect() as db:
        cur = await db.execute('SELECT id, cik, issuer_name, issuer_ticker, source, last_seen_filing_date, created_at, updated_at FROM tracked_companies ORDER BY updated_at DESC LIMIT ?', (limit,))
        rows = await cur.fetchall()
        return [dict(id=r[0], cik=r[1], issuer_name=r[2], issuer_ticker=r[3], source=r[4], last_seen_filing_date=r[5], created_at=r[6], updated_at=r[7]) for r in rows]

async def get_tracked_company_detail(cik: str):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT * FROM tracked_companies WHERE cik = ? LIMIT 1', (cik,))
        company = await cur.fetchone()
        if not company:
            return None
        cur = await db.execute('SELECT * FROM filings WHERE issuer_cik = ? ORDER BY filing_date DESC, id DESC LIMIT 25', (cik,))
        filings = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute('SELECT * FROM alerts WHERE body LIKE ? ORDER BY id DESC LIMIT 25', (f'%{cik}%',))
        alerts = [dict(r) for r in await cur.fetchall()]
        return {'company': dict(company), 'filings': filings, 'alerts': alerts}

async def save_discovery_event(event_key: str, cik: str = None, issuer_name: str = None, form_type: str = None, filing_date: str = None, filing_url: str = None):
    async with _connect() as db:
        try:
            await db.execute('INSERT INTO discovery_events (event_key, cik, issuer_name, form_type, filing_date, filing_url) VALUES (?, ?, ?, ?, ?, ?)', (event_key, cik, issuer_name, form_type, filing_date, filing_url))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def save_filings(items):
    inserted = 0
    async with _connect() as db:
        for item in items:
            uk = filing_unique_key(item)
            try:
                await db.execute('''INSERT INTO filings (
                    accession_number, filing_date, form, issuer_cik, issuer_ticker, issuer_name,
                    reporting_owner_name, reporting_owner_cik, is_director, is_officer, is_ten_percent_owner,
                    is_other, officer_title, transaction_code, transaction_type, security_type, shares,
                    price_per_share, shares_owned_following, ownership_nature, percent_owned, source_url,
                    raw_excerpt, unique_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                    item.accession_number, item.filing_date, item.form, item.issuer_cik, item.issuer_ticker, item.issuer_name,
                    item.reporting_owner_name, item.reporting_owner_cik, int(bool(item.is_director)), int(bool(item.is_officer)), int(bool(item.is_ten_percent_owner)),
                    int(bool(item.is_other)), item.officer_title, item.transaction_code, item.transaction_type, item.security_type, item.shares,
                    item.price_per_share, item.shares_owned_following, item.ownership_nature, item.percent_owned, item.source_url,
                    item.raw_excerpt, uk
                ))
                inserted += 1
            except aiosqlite.IntegrityError:
                pass
        await db.commit()
    return inserted

async def get_filing_detail(unique_key: str):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT * FROM filings WHERE unique_key = ? LIMIT 1', (unique_key,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def save_awards(items):
    inserted = 0
    async with _connect() as db:
        for item in items:
            try:
                await db.execute('''INSERT INTO awards (
                    generated_internal_id, award_id, recipient_name, recipient_uei, awarding_agency,
                    awarding_subagency, action_date, amount, description, naics_code,
                    naics_description, psc_code, psc_description
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                    item.generated_internal_id, item.award_id, item.recipient_name, item.recipient_uei, item.awarding_agency,
                    item.awarding_subagency, item.action_date, item.amount, item.description, item.naics_code,
                    item.naics_description, item.psc_code, item.psc_description
                ))
                inserted += 1
            except aiosqlite.IntegrityError:
                pass
        await db.commit()
    return inserted

async def get_award_detail(generated_internal_id: str):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT * FROM awards WHERE generated_internal_id = ? LIMIT 1', (generated_internal_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def create_alert(alert_type: str, alert_key: str, title: str, body: str):
    async with _connect() as db:
        try:
            await db.execute('INSERT INTO alerts (alert_type, alert_key, title, body) VALUES (?, ?, ?, ?)', (alert_type, alert_key, title, body))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def list_alerts(limit: int = 100):
    async with _connect() as db:
        cur = await db.execute('SELECT id, alert_type, title, body, created_at FROM alerts ORDER BY id DESC LIMIT ?', (limit,))
        rows = await cur.fetchall()
        return [dict(id=r[0], alert_type=r[1], title=r[2], body=r[3], created_at=r[4]) for r in rows]

async def get_alert_detail(alert_id: int):
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute('SELECT * FROM alerts WHERE id = ? LIMIT 1', (alert_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

async def get_settings():
    async with _connect() as db:
        cur = await db.execute('SELECT key, value FROM settings')
        rows = await cur.fetchall()
        return {r[0]: r[1] for r in rows}

async def set_setting(key: str, value: str):
    async with _connect() as db:
        await db.execute('INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP', (key, value))
        await db.commit()

async def get_stats():
    async with _connect() as db:
        out = {}
        for table in ['watchlists', 'tracked_companies', 'discovery_events', 'filings', 'awards', 'alerts']:
            cur = await db.execute(f'SELECT COUNT(*) FROM {table}')
            out[table] = (await cur.fetchone())[0]
        return out
