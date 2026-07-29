"""Gas-swing handling in the forward price forecast.

These pin the pieces that decide how much gas uncertainty reaches the power
price: the volatility term structure, its seasonal shape, the cross-month and
gas-vs-heat-rate correlations, and the recency weighting on implied heat rate.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pjm_core import price_forecast as pf


def _fake_inputs(monkeypatch, n_hist_years=8):
    """Deterministic stand-ins for the EIA/parquet inputs so the engine can be
    exercised without network or a populated data lake."""
    idx = pd.date_range("2018-01-01", periods=n_hist_years * 12, freq="MS")
    gas = pd.Series(3.0 + 0.5 * np.sin(np.arange(len(idx))), index=idx)
    lmp = pd.DataFrame({"month": idx, "lmp": 30.0 + 2 * np.cos(np.arange(len(idx)))})
    monkeypatch.setattr(pf, "_gas_from_eia", lambda *a, **k: gas)
    monkeypatch.setattr(pf, "_load_dom_hub_monthly", lambda *a, **k: lmp)
    monkeypatch.setattr(pf, "_gas_curve",
                        lambda *a, **k: (pd.Series(dtype=float), "test stub"))


# ── Volatility term structure ───────────────────────────────────────────────

def test_ou_vol_saturates_below_sqrt_t():
    """Mean reversion must pull long horizons in; √t growth was the old bug."""
    t = np.array([1 / 12, 1.0, 2.0, 5.0])
    sig = pf._ou_horizon_vol(t, pf.GAS_SIGMA_ANN, pf.GAS_KAPPA)
    rw = pf.GAS_SIGMA_ANN * np.sqrt(t)
    assert np.all(np.diff(sig) > 0)          # still increasing in horizon
    assert sig[-1] < rw[-1] * 0.75           # but well under a random walk
    assert sig[-1] < pf.GAS_SIGMA_ANN / np.sqrt(2 * pf.GAS_KAPPA) + 1e-9


def test_ou_vol_reduces_to_random_walk_without_reversion():
    t = np.array([0.5, 1.0, 2.0])
    got = pf._ou_horizon_vol(t, 0.6, 0.0)
    assert got == pytest.approx(0.6 * np.sqrt(t))


def test_seasonal_risk_is_delivery_month_not_path_multiplier():
    """A far-out January must not inherit a 1.9x multiplier on 18 months of
    accumulated diffusion — that implied a P50 at half the forward."""
    t = np.array([1.5, 1.5])                  # same horizon
    sig = pf._gas_terminal_sigma(t, np.array([1, 7]), pf.SEASONAL_VOL_SHAPE)
    jan, jul = sig
    assert jan > jul                          # winter still carries more risk
    assert jan < 0.85                         # but nothing like 1.9 x base


def test_winter_vol_exceeds_summer_at_equal_horizon():
    t = np.full(12, 0.5)
    sig = pf._gas_terminal_sigma(t, np.arange(1, 13), pf.SEASONAL_VOL_SHAPE)
    assert sig[0] > sig[6]                    # Jan > Jul
    assert sig[11] > sig[4]                   # Dec > May


def test_gas_sigma_is_capped():
    t = np.full(4, 30.0)                      # absurd horizon
    sig = pf._gas_terminal_sigma(t, np.array([1, 1, 1, 1]), {1: 5.0})
    assert np.all(sig <= pf.GAS_SIGMA_MAX + 1e-12)


# ── Seasonal shape calibration ──────────────────────────────────────────────

def test_seasonal_shape_falls_back_on_short_history():
    idx = pd.date_range("2024-01-01", periods=24, freq="MS")
    short = pd.Series(np.linspace(3, 4, 24), index=idx)
    assert pf._seasonal_shape(short) == pf.SEASONAL_VOL_SHAPE
    assert pf._seasonal_shape(pd.Series(dtype=float)) == pf.SEASONAL_VOL_SHAPE


def test_calibrated_seasonal_shape_is_mean_one():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2014-01-01", periods=144, freq="MS")
    # Winter months get extra noise; the fit should recover a >1 winter multiple.
    extra = np.where(np.isin(idx.month, [1, 2, 12]), 0.35, 0.05)
    s = pd.Series(np.exp(np.log(3.0) + rng.normal(0, extra)), index=idx)
    shape = pf._seasonal_shape(s)
    assert len(shape) == 12
    assert np.mean(list(shape.values())) == pytest.approx(1.0, abs=1e-9)
    assert shape[1] > shape[7]


# ── Correlation structure ───────────────────────────────────────────────────

def test_ou_corr_matrix_is_psd_and_decays_with_separation():
    t = np.arange(1, 25) / 12.0
    c = pf._ou_corr_matrix(t, pf.GAS_KAPPA)
    assert np.allclose(np.diag(c), 1.0)
    assert np.allclose(c, c.T)
    assert np.linalg.eigvalsh(c).min() > -1e-10
    assert c[0, 1] > c[0, 6] > c[0, 23]       # further apart, less correlated


def test_equicorr_is_psd():
    m = pf._equicorr(24, pf.HR_TERM_CORR)
    assert np.linalg.eigvalsh(m).min() > -1e-10
    assert m[0, 1] == pytest.approx(pf.HR_TERM_CORR)


@pytest.mark.parametrize("sig_gas,sig_hr", [(0.35, 0.35), (0.15, 0.40), (0.70, 0.18)])
def test_simulated_pass_through_matches_beta_at_any_vol_ratio(monkeypatch,
                                                              sig_gas, sig_hr):
    """Independent lognormals imply power moves 1:1 with gas; DOM Hub passes
    through ~0.90. Parameterising on β rather than a correlation is what keeps
    that number fixed — a fixed correlation would let it drift with σ_hr/σ_gas.
    """
    _fake_inputs(monkeypatch)
    monkeypatch.setattr(pf, "_gas_terminal_sigma",
                        lambda t, *a, **k: np.full(len(t), sig_gas))
    monkeypatch.setattr(pf, "_hr_stats", lambda *a, **k: (12.0, sig_hr, 6.0))
    df, sims = pf.run(horizon_months=4, n_sims=120_000, seed=7,
                      return_paths=True)
    gas_dev = np.log(sims["gas"][:, 2] / df["gas_fwd"].iloc[2])
    beta = np.polyfit(gas_dev, np.log(sims["price"][:, 2]), 1)[0]
    assert beta == pytest.approx(pf.GAS_PASS_THROUGH_BETA, abs=0.02)


def test_correlated_months_widen_the_strip_average():
    """The whole point of the OU path: a horizon average must keep regime risk
    instead of diversifying it away across independent months."""
    rng = np.random.default_rng(3)
    n, ns = 12, 80_000
    sig = 0.4
    c = pf._ou_corr_matrix(np.arange(1, n + 1) / 12.0, pf.GAS_KAPPA)
    corr = rng.standard_normal((ns, n)) @ pf._safe_cholesky(c).T
    indep = rng.standard_normal((ns, n))
    spread = lambda z: np.subtract(*np.percentile(       # noqa: E731
        (3.0 * np.exp(-0.5 * sig ** 2 + sig * z)).mean(axis=1), [90, 10]))
    assert spread(corr) > 1.8 * spread(indep)


# ── Recency-weighted heat rate ──────────────────────────────────────────────

def test_weighted_quantile_matches_median_with_equal_weights():
    v = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    w = np.ones(5)
    assert pf._weighted_quantile(v, w, 0.5) == pytest.approx(3.0)


def test_recency_weighting_tracks_the_recent_fleet():
    """DOM Hub July implied HR ran 15.9, 9.4, 12.8, 18.0, 25.4, 20.6 — pooling
    2020 with 2025 anchors the median well below where the fleet now sits."""
    hr = np.array([15.9, 9.4, 12.8, 18.0, 25.4, 20.6])
    yr = np.array([2020, 2021, 2022, 2023, 2024, 2025])
    med, sigma, n_eff = pf._hr_stats(hr, yr, ref_year=2025)
    assert med > float(np.median(hr))         # pulled up toward recent years
    assert 1.0 < n_eff < len(hr)              # downweighting costs sample size
    assert sigma >= pf.HR_LOG_SIG_FLOOR


def test_hr_stats_falls_back_when_history_too_thin():
    med, sigma, n_eff = pf._hr_stats(np.array([12.0]), np.array([2025]), 2025)
    assert (med, sigma) == (8.0, 0.20)


def test_hr_stats_ignores_nonpositive_samples():
    hr = np.array([-1.0, 0.0, 10.0, 12.0])
    yr = np.array([2022, 2023, 2024, 2025])
    med, _, n_eff = pf._hr_stats(hr, yr, 2025)
    assert 10.0 <= med <= 12.0
    assert n_eff <= 2.0


# ── End-to-end shape ────────────────────────────────────────────────────────

def test_run_returns_ordered_percentiles_and_strip_band(monkeypatch):
    _fake_inputs(monkeypatch)
    df = pf.run(horizon_months=12, n_sims=4_000)
    assert len(df) == 12
    assert (df["p10"] <= df["p50"]).all()
    assert (df["p50"] <= df["p90"]).all()
    assert (df["p10"] < df["p90"]).all()
    for col in ("gas_sigma", "n_eff", "hr_median", "vol_source",
                "strip_p10", "strip_p50", "strip_p90"):
        assert col in df.columns
    assert df["strip_p10"].iloc[0] < df["strip_p50"].iloc[0] < df["strip_p90"].iloc[0]
    assert df["gas_sigma"].iloc[-1] > df["gas_sigma"].iloc[0]   # widens with horizon
    assert (df["p90"] <= pf.PJM_PRICE_CAP).all()


def test_run_is_reproducible_for_a_seed(monkeypatch):
    _fake_inputs(monkeypatch)
    a = pf.run(horizon_months=6, n_sims=2_000, seed=11)
    b = pf.run(horizon_months=6, n_sims=2_000, seed=11)
    pd.testing.assert_frame_equal(a, b)
