from __future__ import annotations

import pytest

from backend.tests.parity_support import (
    canonical_json,
    capture_baseline,
    load_baseline,
    load_market_fixture,
)


@pytest.mark.parametrize(
    ("market", "expected_symbol"),
    [("stocks", "SYNTH"), ("crypto", "SYNTH-USD")],
)
def test_market_fixtures_are_fixed_synthetic_ohlcv(market, expected_symbol):
    fixture = load_market_fixture(market)

    assert fixture["market"] == market
    assert fixture["symbol"] == expected_symbol
    assert fixture["synthetic"] is True
    assert fixture["interval"] == "1d"
    assert len(fixture["bars"]) == 5
    assert all(set(bar) == {"timestamp", "open", "high", "low", "close", "volume"} for bar in fixture["bars"])


def test_market_fixtures_are_independent_copies():
    first = load_market_fixture("stocks")
    first["bars"][0]["close"] = -1

    assert load_market_fixture("stocks")["bars"][0]["close"] == 102.0


def test_fixture_loader_rejects_unknown_market():
    with pytest.raises(ValueError):
        load_market_fixture("options")


def test_canonical_json_orders_keys_and_rejects_non_finite_values():
    assert canonical_json({"z": 1, "a": None}) == '{"a":null,"z":1}'
    with pytest.raises(ValueError):
        canonical_json({"score": float("nan")})


def test_baseline_capture_is_opt_in_and_does_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.delenv("QS_CAPTURE_PARITY_BASELINES", raising=False)
    assert capture_baseline("core", {"score": 42}, directory=tmp_path) is None
    assert not list(tmp_path.iterdir())

    path = capture_baseline("core", {"score": 42}, enabled=True, directory=tmp_path)
    assert path is not None
    assert load_baseline("core", directory=tmp_path) == {"score": 42}
    with pytest.raises(FileExistsError):
        capture_baseline("core", {"score": 99}, enabled=True, directory=tmp_path)
    assert load_baseline("core", directory=tmp_path) == {"score": 42}


def test_baseline_names_cannot_escape_directory(tmp_path):
    with pytest.raises(ValueError):
        capture_baseline("../outside", {}, enabled=True, directory=tmp_path)
