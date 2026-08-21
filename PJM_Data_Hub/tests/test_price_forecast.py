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


# ── Pooled heat-rate model ──────────────────────────────────────────────────

def _hr_panel(n_months=72, trend_per_yr=0.10, seasonal=True,
              start="2020-01-01", noise=0.12, seed=0):
    """A synthetic HR panel with a known log-linear trend and seasonal shape.

    `noise` matters: a noiseless panel fits perfectly, so residual and
    parameter variance both collapse to zero and σ pins to its floor.
    """
    month = pd.date_range(start, periods=n_months, freq="MS")
    t = np.arange(n_months) / 12.0
    seas = 0.3 * np.cos(2 * np.pi * (month.month - 1) / 12) if seasonal else 0.0
    eps = np.random.default_rng(seed).normal(0, noise, n_months)
    hr = np.exp(np.log(10.0) + trend_per_yr * t + seas + eps)
    return pd.DataFrame({"month": month, "cal_month": month.month,
                         "year": month.year, "heat_rate": hr})


def _wx_panel(months, xcold, xhot):
    return pd.DataFrame({"month": months, "xcold": xcold, "xhot": xhot})


def test_fit_returns_none_below_the_minimum_history():
    """A trend trained on two years is worse than no trend — refuse to fit."""
    short = _hr_panel(n_months=pf.HR_MODEL_MIN_MONTHS - 1)
    assert pf._fit_heat_rate_model(short, pd.DataFrame()) is None


def test_fitted_model_recovers_a_known_trend():
    panel = _hr_panel(trend_per_yr=0.10)
    m = pf._fit_heat_rate_model(panel, pd.DataFrame())
    assert m is not None
    assert m.beta[1] == pytest.approx(0.10, abs=0.02)


def test_prediction_extrapolates_the_trend_rather_than_lagging_it():
    """The whole point of pooling: predict forward along the drift, where the
    per-calendar-month median sits back inside the window."""
    panel = _hr_panel(trend_per_yr=0.10)
    m = pf._fit_heat_rate_model(panel, pd.DataFrame())
    nxt = panel["month"].max() + pd.offsets.MonthBegin(1)
    anchor, _ = m.predict([nxt])
    same_cal = panel[panel["cal_month"] == nxt.month]["heat_rate"]
    assert anchor[0] > same_cal.max()


def test_trend_is_held_flat_past_the_extrapolation_horizon():
    """A linear fit run out indefinitely is how the anchor runs away."""
    panel = _hr_panel(trend_per_yr=0.10)
    m = pf._fit_heat_rate_model(panel, pd.DataFrame())
    last = panel["month"].max()
    far = last + pd.DateOffset(years=int(pf.HR_TREND_MAX_YEARS) + 5)
    capped = last + pd.DateOffset(years=int(pf.HR_TREND_MAX_YEARS) + 1)
    # Same calendar month either side, so only the trend term can differ.
    a_far, _ = m.predict([far])
    a_cap, _ = m.predict([pd.Timestamp(capped.year, far.month, 1)])
    assert a_far[0] == pytest.approx(a_cap[0], rel=1e-6)


def test_sigma_grows_with_unknown_weather_risk_in_that_month():
    """Weather is unknowable ahead, so months whose weather varies a lot must
    forecast wider — this is what makes the winter band wider than September's."""
    panel = _hr_panel()
    months = panel["month"]
    rng = np.random.default_rng(0)
    # January swings wildly in cold-day degrees; every other month is placid.
    xcold = np.where(months.dt.month == 1, rng.normal(300, 150, len(months)), 0.0)
    m = pf._fit_heat_rate_model(panel, _wx_panel(months, xcold, 0.0))
    assert m is not None
    jan = pd.Timestamp("2027-01-01")
    sep = pd.Timestamp("2026-09-01")
    _, sig = m.predict([jan, sep])
    assert sig[0] > sig[1]


def test_sigma_widens_as_the_trend_is_extrapolated_further():
    """Parameter uncertainty: the fitted line is least certain furthest out."""
    panel = _hr_panel()
    m = pf._fit_heat_rate_model(panel, pd.DataFrame())
    near = panel["month"].max() + pd.offsets.MonthBegin(1)
    far = near + pd.DateOffset(years=1)
    _, sig = m.predict([near, pd.Timestamp(far.year, near.month, 1)])
    assert sig[1] > sig[0]


def test_run_reports_which_heat_rate_estimator_it_used(monkeypatch):
    _fake_inputs(monkeypatch, n_hist_years=8)
    monkeypatch.setattr(pf, "_load_weather_monthly", lambda *a, **k: pd.DataFrame())
    out = pf.run(horizon_months=6, n_sims=200, seed=1)
    assert "pooled" in out["hr_source"].iloc[0]


def test_run_falls_back_to_the_recency_median_on_short_history(monkeypatch):
    _fake_inputs(monkeypatch, n_hist_years=2)
    monkeypatch.setattr(pf, "_load_weather_monthly", lambda *a, **k: pd.DataFrame())
    out = pf.run(horizon_months=6, n_sims=200, seed=1)
    assert "recency median" in out["hr_source"].iloc[0]


# ── Gas-curve gap filling ───────────────────────────────────────────────────

def _seasonal_strip(start="2026-08-01", n=30, base=3.8):
    """A forward curve with a realistic winter peak / spring trough."""
    month = pd.date_range(start, periods=n, freq="MS")
    mult = {1: 1.26, 2: 1.15, 3: 0.94, 4: 0.89, 5: 0.90, 6: 0.94,
            7: 1.01, 8: 1.03, 9: 0.89, 10: 0.90, 11: 0.95, 12: 1.13}
    return pd.Series([base * mult[m.month] for m in month], index=month)


def test_price_shape_recovers_the_curves_own_seasonality():
    shape = pf._price_seasonal_shape(_seasonal_strip())
    assert shape[1] > shape[4]                      # January over April
    assert np.mean(list(shape.values())) == pytest.approx(1.0, abs=0.01)


def test_price_shape_is_empty_on_a_curve_too_short_to_have_one():
    short = _seasonal_strip(n=6)
    assert pf._price_seasonal_shape(short) == {}


def test_backward_gap_is_not_filled_with_the_far_end_of_the_curve():
    """The old clamp turned 'months before the curve' into 'zero months past
    the end', filling a 2025 slot with the most deferred contract, flat."""
    strip = _seasonal_strip(start="2026-08-01")
    out = pf._extend_to_horizon(strip, 12, pd.Timestamp("2025-08-01"))
    filled = out.loc[:pd.Timestamp("2026-07-01")]
    assert filled.nunique() > 1                      # not flat
    assert filled.max() < strip.max() * 1.5
    # and it carries the seasonal shape, not one repeated number
    assert filled.loc[pd.Timestamp("2026-01-01")] > filled.loc[pd.Timestamp("2026-04-01")]


def test_backward_gap_anchors_on_realised_spot_when_given():
    """Recent spot is far closer to the missing near months than the first
    surviving contract, which can be a year away."""
    strip = _seasonal_strip(start="2026-08-01", base=3.8)
    spot = (pd.Timestamp("2025-07-01"), 2.00)
    out = pf._extend_to_horizon(strip, 12, pd.Timestamp("2025-08-01"), spot=spot)
    assert out.loc[pd.Timestamp("2025-08-01")] < 2.6   # pulled toward spot
    no_spot = pf._extend_to_horizon(strip, 12, pd.Timestamp("2025-08-01"))
    assert out.loc[pd.Timestamp("2025-08-01")] < no_spot.loc[pd.Timestamp("2025-08-01")]


def test_forward_gap_still_mean_reverts_toward_the_long_run_anchor():
    strip = _seasonal_strip(start="2026-08-01", n=13, base=3.0)
    out = pf._extend_to_horizon(strip, 48, pd.Timestamp("2026-08-01"))
    far = out.loc[out.index > strip.index.max()]
    assert abs(far.iloc[-1] - pf.LONG_RUN_GAS) < abs(far.iloc[0] - pf.LONG_RUN_GAS)


def test_months_the_strip_covers_are_passed_through_untouched():
    """The fill must never overwrite a real market print."""
    strip = _seasonal_strip(start="2026-09-01")
    out = pf._extend_to_horizon(strip, 12, pd.Timestamp("2026-08-20"),
                                spot=(pd.Timestamp("2026-07-01"), 2.0))
    for m in pd.date_range("2026-09-01", periods=12, freq="MS"):
        assert out.loc[m] == pytest.approx(float(strip.loc[m]))


# ── Regional gas basis ──────────────────────────────────────────────────────

def _basis_series(years=8, jan=2.5, jul=0.0, noise=0.3, seed=0,
                  end="2026-06-01"):
    """Monthly basis with a winter hump, like the real Virginia series."""
    month = pd.date_range(end=end, periods=years * 12, freq="MS")
    seasonal = np.where(np.isin(month.month, [12, 1, 2]), jan, jul)
    eps = np.random.default_rng(seed).normal(0, noise, len(month))
    return pd.Series(seasonal + eps, index=month)


def test_basis_stats_finds_the_winter_hump():
    mu, sd = pf._basis_stats(_basis_series(), pd.Timestamp("2026-07-01"))
    assert mu[1] > mu[7]
    assert mu[1] == pytest.approx(2.5, abs=0.3)


def test_basis_stats_ignores_history_beyond_the_window():
    """Marcellus reset this market — a 2005 basis should not price 2026."""
    old = pd.Series(6.0, index=pd.date_range("2004-01-01", periods=60, freq="MS"))
    recent = _basis_series()
    mu, _ = pf._basis_stats(pd.concat([old, recent]).sort_index(),
                            pd.Timestamp("2026-07-01"))
    assert max(mu.values()) < 4.0


def test_basis_stats_empty_without_enough_history():
    thin = _basis_series(years=1)
    assert pf._basis_stats(thin, pd.Timestamp("2026-07-01")) == ({}, {})


def test_basis_sigma_is_shrunk_toward_the_pooled_spread():
    """Per-month σ off ~8 points swings wildly; shrinkage keeps the band from
    lurching between adjacent months for no defensible reason."""
    s = _basis_series(noise=0.3)
    # Give one month a wild outlier and check σ does not chase it fully.
    s.loc[s.index[s.index.month == 3][0]] += 6.0
    _, sd = pf._basis_stats(s, pd.Timestamp("2026-07-01"))
    raw = s[s.index.month == 3].std()
    assert sd[3] < raw


def test_delivered_gas_fills_the_publication_lag_with_the_seasonal_normal():
    """EIA runs ~3 months late; the recent months the trend leans on must not
    simply drop out of the panel."""
    gas = pd.Series(3.0, index=pd.date_range("2025-01-01", periods=18, freq="MS"))
    basis = pd.Series(1.0, index=pd.date_range("2025-01-01", periods=12, freq="MS"))
    out = pf._delivered_gas(gas, basis, {m: 1.0 for m in range(1, 13)})
    assert out.notna().all()
    assert out.iloc[-1] == pytest.approx(4.0)


def test_delivered_gas_is_a_passthrough_without_basis_data():
    gas = pd.Series(3.0, index=pd.date_range("2025-01-01", periods=12, freq="MS"))
    out = pf._delivered_gas(gas, pd.Series(dtype=float), {})
    pd.testing.assert_series_equal(out, gas)


def test_basis_is_off_by_default_and_leaves_the_forecast_unchanged(monkeypatch):
    """The channel is opt-in; the default path must not consult it at all."""
    _fake_inputs(monkeypatch, n_hist_years=8)
    monkeypatch.setattr(pf, "_load_weather_monthly", lambda *a, **k: pd.DataFrame())
    called = []
    monkeypatch.setattr(pf, "_load_gas_basis",
                        lambda *a, **k: called.append(1) or pd.Series(dtype=float))
    out = pf.run(horizon_months=6, n_sims=200, seed=1)
    assert not called
    assert out["basis_fwd"].eq(0).all()


def test_basis_widens_the_winter_band_when_enabled(monkeypatch):
    _fake_inputs(monkeypatch, n_hist_years=8)
    monkeypatch.setattr(pf, "_load_weather_monthly", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(pf, "_load_gas_basis", lambda *a, **k: _basis_series())
    out = pf.run(asof=pd.Timestamp("2026-07-01"), horizon_months=12,
                 n_sims=4000, seed=1, use_basis=True)
    jan = out[out["cal_month"] == 1].iloc[0]
    jul = out[out["cal_month"] == 7].iloc[0]
    assert jan["basis_fwd"] > jul["basis_fwd"]
    assert jan["basis_sigma"] > 0
