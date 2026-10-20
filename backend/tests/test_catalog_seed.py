"""Catalog seed/import adapter tests (temp DB only; no universe_service/network)."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.services import catalog_seed as seed  # noqa: E402
from app.services import catalog_service as cs  # noqa: E402

ROWS = [
    {'symbol': 'AAPL', 'name': 'Apple', 'exchange': 'NASDAQ'},
    {'symbol': 'BRK-B', 'name': 'Berkshire B', 'exchange': 'NYSE'},
    {'symbol': 'SPY', 'name': 'SPDR', 'exchange': 'NYSE Arca'},
    {'symbol': 'NOVENUE', 'name': 'x'},
    {'symbol': '', 'exchange': 'NYSE'},
]


def test_adapter_does_not_invent_venue_or_type():
    recs, skipped = seed.universe_rows_to_records(ROWS)
    assert [r['symbol'] for r in recs] == ['AAPL', 'BRK-B', 'SPY']
    assert skipped == {'missing_venue': 1, 'missing_symbol': 1}
    assert all('instrument_type' not in r and r['asset_class'] == 'equity' for r in recs)


def test_crypto_rows_skipped_without_venue_identity():
    recs, skipped = seed.universe_rows_to_records(
        [{'symbol': 'BTC-USD', 'exchange': 'CRYPTO'}], market='crypto')
    assert recs == [] and skipped == {'crypto_venue_unknown': 1}


def test_dry_run_writes_nothing_and_apply_is_idempotent(tmp_path):
    db = str(tmp_path / 'seed.db')
    rep = seed.seed(ROWS, db=db)
    assert rep['applied'] is False and not os.path.exists(db)
    first = seed.seed(ROWS, apply=True, db=db)
    again = seed.seed(ROWS, apply=True, db=db)
    assert first['run']['inserted'] == 3 and again['run']['inserted'] == 0
    assert first['run']['complete'] is False
    c = cs.connect(db)
    assert cs.catalog_counts(c)['total'] == 3
    c.close()


def test_default_seed_is_partial_so_shrunken_list_creates_no_absence(tmp_path):
    db = str(tmp_path / 'seed.db')
    seed.seed(ROWS, apply=True, db=db)
    seed.seed(ROWS[:1], apply=True, db=db)
    c = cs.connect(db)
    assert cs.catalog_counts(c)['by_status'] == {'active': 3}
    c.close()


def test_cli_main_with_file(tmp_path, capsys):
    f = tmp_path / 'rows.json'
    f.write_text(json.dumps(ROWS))
    db = str(tmp_path / 'cli.db')
    assert seed.main(['--universe-file', str(f), '--db', db]) == 0
    assert not os.path.exists(db)
    assert seed.main(['--universe-file', str(f), '--db', db, '--apply']) == 0
    assert json.loads(capsys.readouterr().out.split('\n}\n')[-2] + '\n}')['applied'] is True
