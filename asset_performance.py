"""
Hybrid PV + BESS asset performance engine.

Usage
-----
    python asset_performance.py hybrid_site_scada_8760.csv bess_daily.csv bess_capacity_tests.csv

PV
  - expected output from a PVWatts-style model (Sandia module temperature, -0.35 %/C, 10 % DC losses,
    98 % inverter efficiency, clipped at AC nameplate)
  - performance ratio (PR) and weather-corrected performance index (actual / expected)
  - loss attribution: availability, curtailment, soiling, other
  - ASTM E2848 capacity test on a 14-day window: P = E (a1 + a2 E + a3 Ta + a4 v)
BESS
  - round-trip efficiency with and without auxiliary load, availability, cycles, state of health
"""
import sys

import numpy as np
import pandas as pd

PV_KWDC, PV_KWAC = 5000, 4000
GAMMA, DC_LOSS, INV_EFF = -0.0035, 0.10, 0.98
BESS_KWH = 8000
CAPACITY_GUARANTEE = 0.97


def expected_ac(poa, tamb, wind, kwdc=PV_KWDC, kwac=PV_KWAC):
    t_mod = poa * np.exp(-3.47 - 0.0594 * wind) + tamb
    t_cell = t_mod + poa / 1000 * 3
    pdc = kwdc * poa / 1000 * (1 + GAMMA * (t_cell - 25)) * (1 - DC_LOSS)
    return np.minimum(pdc * INV_EFF, kwac)


def pv_analysis(df: pd.DataFrame) -> dict:
    ts = pd.to_datetime(df.timestamp)
    exp = expected_ac(df.poa_wm2.values, df.tamb_c.values, df.wind_ms.values)
    act = df.pv_ac_kw.values
    av = df.inverter_availability.values
    curt = df.curtailment_setpoint.values
    exp_av = exp * av

    # daily soiling ratio from unconstrained daytime hours, smoothed with a 7-day rolling median
    day = ts.dt.dayofyear.values - 1
    ok = (df.poa_wm2.values > 300) & (curt >= 1) & (exp_av < 0.97 * PV_KWAC)
    num = np.bincount(day[ok], act[ok], minlength=365)
    den = np.bincount(day[ok], exp_av[ok], minlength=365)
    sr = pd.Series(np.where(den > 0, num / np.maximum(den, 1e-9), np.nan)).rolling(7, center=True, min_periods=1).median()
    sr = sr.ffill().bfill().clip(upper=1.0).values
    sr_h = sr[day]

    loss_avail = exp * (1 - av)
    loss_soil = exp_av * (1 - sr_h)
    loss_curt = np.where(curt < 1, np.clip(exp_av * sr_h - act, 0, None), 0)
    month = ts.dt.month.values
    monthly = pd.DataFrame({
        "month": np.arange(1, 13),
        "poa_kwh_m2": np.bincount(month, df.poa_wm2.values, 13)[1:] / 1000,
        "expected_mwh": np.bincount(month, exp, 13)[1:] / 1000,
        "actual_mwh": np.bincount(month, act, 13)[1:] / 1000,
        "availability_loss_mwh": np.bincount(month, loss_avail, 13)[1:] / 1000,
        "curtailment_loss_mwh": np.bincount(month, loss_curt, 13)[1:] / 1000,
        "soiling_loss_mwh": np.bincount(month, loss_soil, 13)[1:] / 1000,
    })
    monthly["other_loss_mwh"] = (monthly.expected_mwh - monthly.actual_mwh - monthly.availability_loss_mwh
                                 - monthly.curtailment_loss_mwh - monthly.soiling_loss_mwh)
    monthly["pr"] = monthly.actual_mwh * 1000 / (PV_KWDC * monthly.poa_kwh_m2)
    monthly["performance_index"] = monthly.actual_mwh / monthly.expected_mwh
    sun = df.poa_wm2.values > 50
    availability = (exp[sun] * av[sun]).sum() / exp[sun].sum()
    return {"monthly": monthly, "soiling_ratio_daily": sr, "availability": availability,
            "expected_mwh": exp.sum() / 1000, "actual_mwh": act.sum() / 1000}


def astm_e2848(df: pd.DataFrame, start="2025-08-18", end="2025-08-31") -> dict:
    """Regression capacity test. Filters: POA >= 400 W/m2, full availability, no curtailment,
    below 98 % of AC nameplate (excludes clipping). Reporting conditions from filtered data,
    then POA held to 80-120 % of RC irradiance."""
    ts = pd.to_datetime(df.timestamp)
    w = df[(ts >= start) & (ts <= pd.Timestamp(end) + pd.Timedelta(hours=23))].copy()
    w["model_kw"] = expected_ac(w.poa_wm2.values, w.tamb_c.values, w.wind_ms.values)
    f = w[(w.poa_wm2 >= 400) & (w.inverter_availability >= 1) & (w.curtailment_setpoint >= 1)
          & (w.pv_ac_kw < 0.98 * PV_KWAC) & (w.model_kw < 0.98 * PV_KWAC)]
    rc = {"poa": float(f.poa_wm2.quantile(0.6)), "tamb": float(f.tamb_c.mean()), "wind": float(f.wind_ms.mean())}
    f = f[(f.poa_wm2 >= 0.8 * rc["poa"]) & (f.poa_wm2 <= 1.2 * rc["poa"])]
    E, T, V = f.poa_wm2.values, f.tamb_c.values, f.wind_ms.values
    X = np.column_stack([E, E * E, E * T, E * V])

    def fit(y):
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        pred = X @ coef
        r2 = 1 - ((y - pred) ** 2).sum() / ((y - y.mean()) ** 2).sum()
        p_rc = rc["poa"] * (coef[0] + coef[1] * rc["poa"] + coef[2] * rc["tamb"] + coef[3] * rc["wind"])
        return coef, r2, p_rc, pred

    c_m, r2_m, p_meas, pred = fit(f.pv_ac_kw.values)
    c_x, r2_x, p_model, _ = fit(f.model_kw.values)
    ratio = p_meas / p_model
    return {"rc": rc, "points": len(f), "coef_measured": c_m, "r2": r2_m, "p_measured_rc_kw": p_meas,
            "p_expected_rc_kw": p_model, "capacity_ratio": ratio, "pass": ratio >= CAPACITY_GUARANTEE}


def bess_analysis(daily: pd.DataFrame, tests: pd.DataFrame) -> dict:
    d = daily.copy()
    d["month"] = pd.to_datetime(d.date).dt.month
    m = d.groupby("month").sum(numeric_only=True)
    days = d.groupby("month").size()
    out = pd.DataFrame({
        "charge_mwh": m.charge_kwh / 1000,
        "discharge_mwh": m.discharge_kwh / 1000,
        "rte_dc": m.discharge_kwh / m.charge_kwh,
        "rte_with_aux": m.discharge_kwh / (m.charge_kwh + m.aux_kwh),
        "availability": m.available / days,
        "cycles": m.discharge_kwh / BESS_KWH,
    })
    soh = tests.usable_energy_kwh / tests.nameplate_kwh
    return {"monthly": out, "rte_dc": d.discharge_kwh.sum() / d.charge_kwh.sum(),
            "rte_with_aux": d.discharge_kwh.sum() / (d.charge_kwh.sum() + d.aux_kwh.sum()),
            "availability": d.available.mean(), "cycles": d.discharge_kwh.sum() / BESS_KWH,
            "soh": soh.tolist()}


def main():
    scada = pd.read_csv(sys.argv[1] if len(sys.argv) > 1 else "hybrid_site_scada_8760.csv")
    daily = pd.read_csv(sys.argv[2] if len(sys.argv) > 2 else "bess_daily.csv")
    tests = pd.read_csv(sys.argv[3] if len(sys.argv) > 3 else "bess_capacity_tests.csv")
    pd.set_option("display.width", 200)
    pv = pv_analysis(scada)
    print(f"PV expected {pv['expected_mwh']:,.0f} MWh, actual {pv['actual_mwh']:,.0f} MWh, "
          f"availability {pv['availability']:.2%}")
    print(pv["monthly"].round(3).to_string(index=False))
    t = astm_e2848(scada)
    print(f"\nASTM E2848: RC {t['rc']['poa']:.0f} W/m2, {t['rc']['tamb']:.1f} C, {t['rc']['wind']:.1f} m/s | "
          f"n={t['points']} R2={t['r2']:.3f} | measured {t['p_measured_rc_kw']:,.0f} kW vs expected "
          f"{t['p_expected_rc_kw']:,.0f} kW -> ratio {t['capacity_ratio']:.3f} ({'PASS' if t['pass'] else 'FAIL'})")
    b = bess_analysis(daily, tests)
    print(f"\nBESS RTE {b['rte_dc']:.1%} (with aux {b['rte_with_aux']:.1%}), availability {b['availability']:.1%}, "
          f"{b['cycles']:.0f} cycles, SOH {b['soh']}")
    print(b["monthly"].round(3).to_string())


if __name__ == "__main__":
    main()
