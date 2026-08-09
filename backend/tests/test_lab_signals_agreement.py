"""
Unit tests for model_agreement_certainty / validate_model_agreement_certainty.

Run with:  pytest backend/tests/test_lab_signals_agreement.py -v

No network access required — all inputs are synthetic.
"""
import math
import pytest
from app.services.lab_signals import (
    model_agreement_certainty,
    quantum_interference_certainty,   # backward-compat alias
    validate_model_agreement_certainty,
)


class TestModelAgreementCertainty:
    """Deterministic property tests matching the validate_* docstring."""

    def test_both_agree_bullish(self):
        # a_fast = √(2·0.30) ≈ 0.7746, a_g = √(2·0.25) ≈ 0.7071
        # combined ≈ 1.0477  →  squared ≈ 1.097  →  clipped 1.0
        assert model_agreement_certainty(0.80, 0.75) == pytest.approx(1.0, abs=1e-9)

    def test_both_agree_bearish(self):
        # Same magnitudes as bullish case, same direction → same output.
        assert model_agreement_certainty(0.20, 0.25) == pytest.approx(1.0, abs=1e-9)

    def test_perfect_cancel(self):
        # Equal and opposite conviction — magnitudes cancel exactly.
        assert model_agreement_certainty(0.75, 0.25) == pytest.approx(0.0, abs=1e-9)

    def test_partial_disagree(self):
        # fast bullish 0.70, garch bearish 0.40
        a_f = math.sqrt(2.0 * 0.20)
        a_g = math.sqrt(2.0 * 0.10)
        expected = ((a_f - a_g) / math.sqrt(2.0)) ** 2
        assert model_agreement_certainty(0.70, 0.40) == pytest.approx(expected, abs=1e-9)

    def test_single_model_fallback(self):
        # certainty = 2·|p−0.5| = 2·0.30 = 0.60
        assert model_agreement_certainty(0.80, None) == pytest.approx(0.60, abs=1e-9)

    def test_at_50pct_certainty_zero(self):
        assert model_agreement_certainty(0.50, 0.50) == pytest.approx(0.0, abs=1e-9)

    def test_output_bounded(self):
        # Output must always be in [0, 1] for any valid probability pair.
        for p1 in [0.0, 0.1, 0.5, 0.9, 1.0]:
            for p2 in [0.0, 0.1, 0.5, 0.9, 1.0, None]:
                v = model_agreement_certainty(p1, p2)
                assert 0.0 <= v <= 1.0, f"out of bounds for ({p1}, {p2}): {v}"

    def test_backward_compat_alias(self):
        # quantum_interference_certainty must be the same function.
        assert quantum_interference_certainty is model_agreement_certainty


class TestValidateModelAgreementCertainty:
    """Validate the built-in validation suite passes end-to-end."""

    def test_all_cases_pass(self):
        report = validate_model_agreement_certainty()
        assert report.get('all_pass') is True, (
            f"Some validation cases failed:\n"
            + "\n".join(
                f"  {k}: {v}"
                for k, v in report.items()
                if k != 'all_pass' and isinstance(v, dict) and not v.get('pass')
            )
        )
