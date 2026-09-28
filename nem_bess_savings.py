"""
NEM 2.0 -> NEM 3.0 (Net Billing Tariff) solar + BESS savings engine.

Usage
-----
    python nem_bess_savings.py sample_site_8760.csv
    python nem_bess_savings.py my_site.csv --simulate-battery --battery-kw 500 --battery-kwh 2000
    python nem_bess_savings.py --load utility_8760.csv --solar solar_8760.csv --battery etb_export.csv

Input CSV (hourly 8760 or 15-minute 35040 rows), column names are matched loosely:
    timestamp                     e.g. 2025-01-01 00:00
    load_kwh                      site consumption in the interval
    solar_kwh                     PV production in the interval (ETB "Solar Production")
    battery_kwh        optional   + discharge to site / - charge (ETB battery output), or
    battery_discharge_kwh + battery_charge_kwh   as two positive columns

Scenarios
---------
    A  Baseline: no solar
    B  Solar only, NEM 2.0   exports credited at retail TOU rate minus non-bypassable charges
    C  Solar only, NEM 3.0   exports credited at hourly ACC export values
    D  Solar + BESS, NEM 3.0 battery dispatch from the CSV (Energy Toolbase) or the built-in dispatch

Rates are ILLUSTRATIVE, modelled on the structure of a PG&E B-19 secondary TOU schedule. Replace
TARIFF and the ACC matrix with the current tariff sheet and the customer's ACC vintage before use.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ tariff (illustrative)
TARIFF = {
    "name": "Illustrative B-19-style secondary TOU",
    "summer_months": [6, 7, 8, 9],
    "energy": {  # $/kWh
        "summer": {"peak": 0.215, "part": 0.178, "off": 0.152},
        "winter": {"peak": 0.188, "off": 0.150, "super": 0.112},
    },
    "demand": {  # $/kW-month
        "max": 33.50,
        "summer": {"peak": 38.10, "part": 7.20},
        "winter": {"peak": 2.60},
    },
    "customer_charge": 1350.0,  # $/month
    "nbc": 0.028,               # non-bypassable charges, $/kWh (NEM 2.0 export credit reduction)
    "nsc": 0.040,               # net surplus compensation at annual true-up, $/kWh
}


def tou_period(month: np.ndarray, hour: np.ndarray, summer_months) -> tuple[np.ndarray, np.ndarray]:
    """Return (season, period) arrays. Peak 4-9pm daily; summer part-peak 2-4pm & 9-11pm;
    winter super-off-peak 9am-2pm."""
    summer = np.isin(month, summer_months)
    period = np.full(month.shape, "off", dtype=object)
    period[(hour >= 16) & (hour < 21)] = "peak"
    period[summer & (((hour >= 14) & (hour < 16)) | ((hour >= 21) & (hour < 23)))] = "part"
    period[~summer & (hour >= 9) & (hour < 14)] = "super"
    season = np.where(summer, "summer", "winter")
    return season, period


def default_acc() -> np.ndarray:
    """Illustrative 12 x 24 ACC export value matrix ($/kWh). Low midday (solar saturation),
    high summer evenings (capacity value). Replace with the utility's published ACC table."""
    g = np.array([0.055] * 7 + [0.045, 0.040, 0.034, 0.030, 0.028, 0.028, 0.030, 0.038, 0.050,
                                0.070, 0.100, 0.150, 0.180, 0.120, 0.090, 0.070, 0.060])
    evening_mult = np.array([1.0, 1.0, 1.0, 1.0, 1.1, 1.3, 2.0, 5.0, 10.0, 1.6, 1.0, 1.0])
    acc = np.tile(g, (12, 1))
    for m in range(12):
        acc[m, 17:21] *= evening_mult[m]
    return np.round(acc, 4)


# ------------------------------------------------------------------ battery
@dataclass
class BatteryConfig:
    power_kw: float = 500
    energy_kwh: float = 2000
    rte: float = 0.88
    min_soc: float = 0.05
    demand_cap_kw: float | None = None  # shave net import above this (None = no shaving)


def peak_level(net_window, energy, p_int) -> float:
    """Lowest import level L such that sum(min(max(net - L, 0), p_int)) <= energy (bisection)."""
    net_window = np.clip(net_window, 0, None)
    if len(net_window) == 0 or energy <= 0:
        return float(net_window.max()) if len(net_window) else 0.0
    lo, hi = 0.0, float(net_window.max())
    for _ in range(40):
        mid = (lo + hi) / 2
        used = np.minimum(np.clip(net_window - mid, 0, None), p_int).sum()
        if used > energy:
            lo = mid
        else:
            hi = mid
    return hi


def simulate_dispatch(load, solar, bat: BatteryConfig, hour, dt: float = 1.0) -> np.ndarray:
    """Rule-based NBT dispatch (stand-in for an Energy Toolbase run).
    1. charge only from excess solar (keeps ITC-eligible, avoids low-value exports)
    2. discharge to hold net import at or below the demand cap
    3. through the 4-9pm peak, flatten net import to the lowest level the stored energy can hold
       for the whole window (day-ahead water-fill, like an optimizer with a perfect load forecast)
    Returns battery energy per interval: + discharge, - charge (kWh at the meter)."""
    eta = np.sqrt(bat.rte)
    e_max, e_min = bat.energy_kwh, bat.energy_kwh * bat.min_soc
    soc = e_min
    p_int = bat.power_kw * dt
    net_all = np.asarray(load, float) - np.asarray(solar, float)
    n = len(net_all)
    out = np.zeros(n)
    level = 0.0
    for i in range(n):
        net = net_all[i]
        in_peak = 16 <= hour[i] < 21
        if in_peak and (i == 0 or not (16 <= hour[i - 1] < 21)):
            j = i
            while j < n and 16 <= hour[j] < 21:
                j += 1
            level = peak_level(net_all[i:j], (soc - e_min) * eta, p_int)
        if net < 0:
            c = min(-net, p_int, (e_max - soc) / eta)
            soc += c * eta
            out[i] = -c
        else:
            need = 0.0
            if bat.demand_cap_kw is not None and net / dt > bat.demand_cap_kw:
                need = net - bat.demand_cap_kw * dt
            if in_peak:
                need = max(need, net - level)
            d = min(need, p_int, (soc - e_min) * eta)
            if d > 0:
                soc -= d / eta
                out[i] = d
    return out


# ------------------------------------------------------------------ input
def _find(cols, *keys):
    for c in cols:
        lc = c.lower().replace(" ", "_")
        if all(k in lc for k in keys):
            return c
    return None


def load_site_csv(path: str) -> pd.DataFrame:
    raw = pd.read_csv(path)
    cols = list(raw.columns)
    ts = _find(cols, "time") or _find(cols, "date") or cols[0]
    load = _find(cols, "load") or _find(cols, "consum") or _find(cols, "usage")
    solar = _find(cols, "solar") or _find(cols, "pv")
    if load is None or solar is None:
        raise ValueError(f"Need load and solar columns, found {cols}")
    df = pd.DataFrame({"timestamp": pd.to_datetime(raw[ts]), "load": raw[load].astype(float),
                       "solar": raw[solar].astype(float)})
    batt = _battery_series(raw)
    if batt is not None:
        df["battery"] = batt
    return df


def _battery_series(raw: pd.DataFrame):
    """ETB battery output: separate discharge/charge columns, or one signed column (+ discharge)."""
    cols = list(raw.columns)
    dis = _find(cols, "dischar")
    chg = next((c for c in cols if "charg" in c.lower() and "dis" not in c.lower()), None)
    if dis and chg:
        return raw[dis].astype(float).abs().values - raw[chg].astype(float).abs().values
    one = _find(cols, "batt") or _find(cols, "bess") or _find(cols, "storage")
    return raw[one].astype(float).values if one else None


def _series(path: str, *keys) -> tuple[pd.Series, np.ndarray]:
    raw = pd.read_csv(path)
    cols = list(raw.columns)
    ts = pd.to_datetime(raw[_find(cols, "time") or _find(cols, "date") or cols[0]])
    for k in keys:
        c = _find(cols, k)
        if c:
            return ts, raw[c].astype(float).values
    num = [c for c in cols[1:] if pd.api.types.is_numeric_dtype(raw[c])]
    if len(num) == 1:
        return ts, raw[num[0]].astype(float).values
    raise ValueError(f"{path}: cannot tell which column to use, found {cols}")


def _align(x: np.ndarray, n: int) -> np.ndarray:
    """Hourly -> 15-min (split kWh in four), 15-min -> hourly (sum), or pad/trim a leap day."""
    m = len(x)
    if m == n:
        return x
    if m * 4 == n:
        return np.repeat(x, 4) / 4
    if m == n * 4:
        return x.reshape(n, 4).sum(axis=1)
    per_day = 96 if n > 9000 else 24
    if m + per_day == n:
        return np.concatenate([x, x[-per_day:]])
    if m == n + per_day:
        return x[:n]
    raise ValueError(f"{m} rows do not line up with {n} load rows")


def build_site(load_csv: str, solar_csv: str, battery_csv: str | None = None) -> pd.DataFrame:
    """Separate files: utility load interval data, solar 8760, optional Energy Toolbase battery export."""
    ts, load = _series(load_csv, "load", "consum", "usage", "demand", "kwh")
    _, solar = _series(solar_csv, "solar", "pv", "produc", "generat", "kwh")
    df = pd.DataFrame({"timestamp": ts, "load": load, "solar": _align(solar, len(load))})
    if battery_csv:
        batt = _battery_series(pd.read_csv(battery_csv))
        if batt is None:
            raise ValueError(f"{battery_csv}: no battery, discharge/charge or storage column")
        df["battery"] = _align(batt, len(load))
    return df


def interval_hours(df: pd.DataFrame) -> float:
    return {8760: 1.0, 8784: 1.0, 35040: 0.25, 35136: 0.25}.get(
        len(df), (df.timestamp.iloc[1] - df.timestamp.iloc[0]).total_seconds() / 3600)


# ------------------------------------------------------------------ billing
def bill(df: pd.DataFrame, net_kwh: np.ndarray, export_mode: str | None, tariff=TARIFF, acc=None,
         dt: float = 1.0) -> dict:
    """Annual bill with monthly detail. net_kwh > 0 import, < 0 export.
    export_mode: None (no exports expected), 'nem2', or 'nbt'."""
    acc = default_acc() if acc is None else acc
    month = df.timestamp.dt.month.values
    hour = df.timestamp.dt.hour.values
    season, period = tou_period(month, hour, tariff["summer_months"])
    rate = np.array([tariff["energy"][s][p] for s, p in zip(season, period)])
    imp = np.clip(net_kwh, 0, None)
    exp = np.clip(-net_kwh, 0, None)
    if export_mode == "nbt":
        ex_rate = acc[month - 1, hour]
    else:
        ex_rate = np.maximum(rate - tariff["nbc"], 0)
    kw = imp / dt

    months = []
    for m in range(1, 13):
        sel = month == m
        summer = m in tariff["summer_months"]
        pk = sel & (period == "peak")
        pp = sel & (period == "part")
        d_max = kw[sel].max()
        d_pk = kw[pk].max() if pk.any() else 0.0
        d_pp = kw[pp].max() if pp.any() else 0.0
        dem = tariff["demand"]
        rec = {
            "month": m,
            "import_kwh": imp[sel].sum(), "export_kwh": exp[sel].sum(),
            "energy_cost": (imp[sel] * rate[sel]).sum(),
            "export_credit": (exp[sel] * ex_rate[sel]).sum(),
            "max_kw": d_max, "peak_kw": d_pk,
            "demand_max": d_max * dem["max"],
            "demand_peak": d_pk * (dem["summer"]["peak"] if summer else dem["winter"]["peak"]),
            "demand_part": d_pp * dem["summer"]["part"] if summer else 0.0,
            "fixed": tariff["customer_charge"],
        }
        rec["demand_cost"] = rec["demand_max"] + rec["demand_peak"] + rec["demand_part"]
        months.append(rec)
    mdf = pd.DataFrame(months)

    # credits roll month to month and only offset energy charges; annual true-up
    net_energy = mdf.energy_cost.sum() - mdf.export_credit.sum()
    forfeited = max(-net_energy, 0.0)
    surplus_kwh = max(mdf.export_kwh.sum() - mdf.import_kwh.sum(), 0.0)
    nsc_credit = surplus_kwh * tariff["nsc"]
    energy_net = max(net_energy, 0.0) - nsc_credit
    total = energy_net + mdf.demand_cost.sum() + mdf.fixed.sum()
    return {
        "monthly": mdf,
        "energy_cost": mdf.energy_cost.sum(),
        "export_credit": mdf.export_credit.sum() - forfeited,
        "forfeited_credit": forfeited,
        "nsc_credit": nsc_credit,
        "energy_net": energy_net,
        "demand_cost": mdf.demand_cost.sum(),
        "fixed": mdf.fixed.sum(),
        "total": total,
        "import_kwh": mdf.import_kwh.sum(),
        "export_kwh": mdf.export_kwh.sum(),
    }


def run(df: pd.DataFrame, tariff=TARIFF, acc=None, battery: BatteryConfig | None = None) -> dict:
    dt = interval_hours(df)
    load, solar = df.load.values, df.solar.values
    if "battery" in df and battery is None:
        batt = df.battery.values
    else:
        batt = simulate_dispatch(load, solar, battery or BatteryConfig(), df.timestamp.dt.hour.values, dt)
    res = {
        "A": bill(df, load, None, tariff, acc, dt),
        "B": bill(df, load - solar, "nem2", tariff, acc, dt),
        "C": bill(df, load - solar, "nbt", tariff, acc, dt),
        "D": bill(df, load - solar - batt, "nbt", tariff, acc, dt),
    }
    res["battery"] = batt
    return res


def summarize(res: dict) -> pd.DataFrame:
    base = res["A"]
    names = {"A": "Baseline (no solar)", "B": "Solar, NEM 2.0", "C": "Solar, NEM 3.0",
             "D": "Solar + BESS, NEM 3.0"}
    rows = []
    for k, label in names.items():
        r = res[k]
        rows.append({
            "scenario": label,
            "annual_bill": r["total"],
            "energy_net": r["energy_net"],
            "demand": r["demand_cost"],
            "import_mwh": r["import_kwh"] / 1000,
            "export_mwh": r["export_kwh"] / 1000,
            "energy_savings": base["energy_net"] - r["energy_net"],
            "demand_savings": base["demand_cost"] - r["demand_cost"],
            "total_savings": base["total"] - r["total"],
        })
    return pd.DataFrame(rows).set_index("scenario")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="one combined file with load, solar and optional battery columns")
    ap.add_argument("--load", help="utility load interval data (8760 hourly or 35040 15-minute)")
    ap.add_argument("--solar", help="solar production 8760")
    ap.add_argument("--battery", help="Energy Toolbase battery export (optional)")
    ap.add_argument("--simulate-battery", action="store_true", help="ignore battery column, use built-in dispatch")
    ap.add_argument("--battery-kw", type=float, default=500)
    ap.add_argument("--battery-kwh", type=float, default=2000)
    ap.add_argument("--demand-cap-kw", type=float, default=None)
    a = ap.parse_args()
    if a.load:
        if not a.solar:
            ap.error("--solar is required with --load")
        df = build_site(a.load, a.solar, a.battery)
    elif a.csv:
        df = load_site_csv(a.csv)
    else:
        ap.error("give a combined CSV or --load and --solar")
    bat = BatteryConfig(a.battery_kw, a.battery_kwh, demand_cap_kw=a.demand_cap_kw) \
        if (a.simulate_battery or "battery" not in df) else None
    res = run(df, battery=bat)
    pd.set_option("display.float_format", lambda v: f"{v:,.0f}")
    print(summarize(res).T)


if __name__ == "__main__":
    main()
