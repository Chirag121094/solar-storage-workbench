"""
Generate the synthetic sample data used by the portfolio dashboards.

Nothing here is real client data. It produces:
  sample_site_8760.csv       - commercial site: hourly load, solar (1,500 kWdc), battery dispatch
                               in the same column layout the NEM tool expects from Energy Toolbase
  hybrid_site_scada_8760.csv - hybrid PV+BESS plant: hourly POA, ambient temp, wind, PV AC output,
                               inverter availability, curtailment setpoint
  bess_daily.csv             - hybrid plant BESS daily charge / discharge / aux / availability
  bess_capacity_tests.csv    - quarterly usable-energy capacity tests
  site_data.json             - compact copy of the above, embedded in the website
"""
import json
import numpy as np
import pandas as pd

from nem_bess_savings import BatteryConfig, simulate_dispatch

RNG = np.random.default_rng(2848)
YEAR = 2025
LAT = 36.7          # San Joaquin Valley, CA
TILT = 20.0
GAMMA = -0.0035     # Pmp temperature coefficient, 1/degC
DC_LOSS = 0.10      # soiling-free DC losses: mismatch, wiring, LID, nameplate
INV_EFF = 0.98
DC_AC = 1.25

idx = pd.date_range(f"{YEAR}-01-01 00:00", periods=8760, freq="h")
n = idx.dayofyear.values
hour = idx.hour.values
month = idx.month.values

# ---------------------------------------------------------------- weather
decl = np.radians(23.45 * np.sin(np.radians(360 / 365 * (284 + n))))
omega = np.radians(15 * (hour + 0.5 - 12))
phi, beta = np.radians(LAT), np.radians(TILT)
cosz = np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(omega)
cosz_c = np.clip(cosz, 0, None)
ghi_clear = np.where(cosz > 0.02, 1098 * cosz_c * np.exp(-0.057 / np.maximum(cosz_c, 1e-3)), 0)

kt_month = np.array([0.62, 0.72, 0.80, 0.88, 0.93, 0.97, 0.98, 0.97, 0.95, 0.88, 0.72, 0.60])
sd_month = np.array([0.22, 0.18, 0.15, 0.10, 0.06, 0.03, 0.03, 0.03, 0.05, 0.10, 0.18, 0.22])
dmon = pd.date_range(f"{YEAR}-01-01", periods=365).month.values - 1
day_f = np.clip(RNG.normal(kt_month[dmon], sd_month[dmon]), 0.12, 1.0)
f = day_f[n - 1] * np.clip(RNG.normal(1, 0.03, 8760), 0.85, 1.05)
ghi = ghi_clear * np.clip(f, 0, 1)
b = 0.82 * np.clip((f - 0.3) / 0.7, 0, 1)                  # beam fraction falls with cloudiness
dni = np.where(cosz > 0.05, np.minimum(ghi * b / np.maximum(cosz_c, 0.05), 1000), 0)
dhi = ghi - dni * cosz_c
cos_th = np.sin(decl) * np.sin(phi - beta) + np.cos(decl) * np.cos(phi - beta) * np.cos(omega)
poa = dni * np.clip(cos_th, 0, None) + dhi * (1 + np.cos(beta)) / 2 + ghi * 0.2 * (1 - np.cos(beta)) / 2
poa = np.round(np.clip(poa, 0, None), 0)

t_mean = np.array([8, 11, 14, 17, 22, 27, 30, 29, 26, 20, 13, 8], float)
t_amp = np.array([5, 6, 7, 8, 9, 10, 10, 10, 9, 8, 6, 5], float)
day_dt = RNG.normal(0, 2.0, 365)
tamb = t_mean[month - 1] + day_dt[n - 1] + t_amp[month - 1] * np.cos(np.radians(15 * (hour - 15)))
tamb = np.round(tamb, 1)
wind = np.round(np.clip(2.0 + 1.6 * np.clip(np.sin(np.radians(15 * (hour - 9))), 0, None)
                        + RNG.normal(0, 0.6, 8760), 0.3, None), 1)


def pv_ac_per_kwdc(poa, tamb, wind):
    """PVWatts-style expected AC output per kWdc (kW). Sandia module temperature model."""
    t_mod = poa * np.exp(-3.47 - 0.0594 * wind) + tamb
    t_cell = t_mod + poa / 1000 * 3
    pdc = poa / 1000 * (1 + GAMMA * (t_cell - 25)) * (1 - DC_LOSS)
    return np.minimum(pdc * INV_EFF, 1 / DC_AC)


solar_unit = pv_ac_per_kwdc(poa, tamb, wind)

# ---------------------------------------------------------------- commercial site load
dow = idx.dayofweek.values  # Mon=0
wk = dow < 5
sat = dow == 5
ops = np.zeros(8760)
prof = {6: 150, 7: 280, 8: 290, 9: 290, 10: 290, 11: 290, 12: 270, 13: 290, 14: 290, 15: 290,
        16: 280, 17: 220, 18: 120, 19: 90, 20: 80, 21: 40}
for h, v in prof.items():
    ops[wk & (hour == h)] = v
for h in range(6, 15):
    ops[sat & (hour == h)] = 120
refrig = 90 + 7.0 * np.clip(tamb - 15, 0, None)
load = (170 + refrig + ops) * RNG.normal(1, 0.04, 8760)
load = np.round(load, 1)

SOLAR_KWDC = 1500
solar = np.round(solar_unit * SOLAR_KWDC, 2)
bat = BatteryConfig(power_kw=500, energy_kwh=2000, rte=0.88, min_soc=0.05, demand_cap_kw=None)
dispatch = simulate_dispatch(load, solar, bat, hour=hour, dt=1.0)

site = pd.DataFrame({
    "timestamp": idx.strftime("%Y-%m-%d %H:%M"),
    "load_kwh": load,
    "solar_kwh": solar,
    "battery_kwh": np.round(dispatch, 2),   # + discharge to site, - charge
})
site.to_csv("sample_site_8760.csv", index=False)

# ---------------------------------------------------------------- hybrid plant SCADA
PV_KWDC, PV_KWAC, N_INV = 5000, 4000, 20
expected = pv_ac_per_kwdc(poa, tamb, wind) * PV_KWDC

# inverter availability: one inverter out Jul 8-17, two out for a day in Feb and Nov
avail = np.ones(8760)
avail[(month == 7) & (idx.day >= 8) & (idx.day <= 17)] = 1 - 1 / N_INV
avail[(month == 2) & (idx.day == 11)] = 1 - 2 / N_INV
avail[(month == 11) & (idx.day == 20)] = 1 - 2 / N_INV

# soiling: accumulates in the dry season, rain resets in Nov/Jan, wash on Aug 15
soil = np.zeros(8760)
s = 0.0
for i, ts in enumerate(idx):
    if ts.hour == 0:
        m = ts.month
        s += 0.0004 if m in (4, 5, 6, 7, 8, 9, 10) else 0.0001
        if (m == 8 and ts.day == 15) or (m == 11 and ts.day == 12) or (m == 1 and ts.day == 20) \
                or (m == 3 and ts.day == 3):
            s = 0.0
    soil[i] = s
soil = np.minimum(soil, 0.08)

# CAISO midday curtailment in spring (setpoint as % of AC nameplate)
curt = np.ones(8760)
spring_days = RNG.choice(np.where(np.isin(pd.date_range(f"{YEAR}-01-01", periods=365).month, [3, 4, 5]))[0],
                         24, replace=False)
for d in spring_days:
    curt[(n - 1 == d) & (hour >= 10) & (hour <= 14)] = 0.6

actual = expected * avail * (1 - soil) * RNG.normal(1, 0.012, 8760)
actual = np.minimum(actual, curt * PV_KWAC)
actual = np.where(poa > 0, np.clip(actual, 0, None), 0)
actual = np.round(actual, 1)

scada = pd.DataFrame({
    "timestamp": idx.strftime("%Y-%m-%d %H:%M"),
    "poa_wm2": poa, "tamb_c": tamb, "wind_ms": wind,
    "pv_ac_kw": actual,
    "inverter_availability": np.round(avail, 3),
    "curtailment_setpoint": curt,
})
scada.to_csv("hybrid_site_scada_8760.csv", index=False)

# ---------------------------------------------------------------- hybrid plant BESS (2 MW / 8 MWh)
days = pd.date_range(f"{YEAR}-01-01", periods=365)
dm = days.month.values
cap_q = np.array([8000, 7935, 7872, 7810])            # usable kWh at each quarterly test
cap_day = np.interp(np.arange(365), [0, 90, 181, 273, 364], [8000, 7935, 7872, 7810, 7760])
rte_dc = np.array([0.905, 0.905, 0.903, 0.900, 0.897, 0.893, 0.890, 0.889, 0.892, 0.897, 0.902, 0.904])
aux_kwh = np.array([140, 140, 150, 180, 240, 320, 380, 370, 300, 210, 160, 140], float)  # HVAC + controls
bess_avail = np.ones(365)
bess_avail[(dm == 9) & (days.day >= 3) & (days.day <= 5)] = 0      # PCS fault
cycles = np.clip(RNG.normal(0.92, 0.06, 365), 0.6, 1.0)
discharge = np.round(cap_day * cycles * bess_avail, 0)
charge = np.round(discharge / rte_dc[dm - 1] * RNG.normal(1, 0.004, 365), 0)
aux = np.round(aux_kwh[dm - 1] * RNG.normal(1, 0.05, 365), 0)
bess = pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "charge_kwh": charge, "discharge_kwh": discharge,
                     "aux_kwh": aux, "available": bess_avail.astype(int)})
bess.to_csv("bess_daily.csv", index=False)
tests = pd.DataFrame({"date": ["2025-01-06", "2025-04-07", "2025-07-07", "2025-10-06"],
                      "usable_energy_kwh": cap_q, "nameplate_kwh": 8000})
tests.to_csv("bess_capacity_tests.csv", index=False)

# ---------------------------------------------------------------- compact json for the site
out = {
    "load": load.tolist(),                      # kWh, same values as sample_site_8760.csv
    "solar1500": solar.tolist(),                # kWh for 1,500 kWdc; the site scales it by PV size
    "poa": poa.astype(int).tolist(),
    "tamb": tamb.tolist(),
    "wind": wind.tolist(),
    "pv": np.round(actual).astype(int).tolist(),
    "avail": np.round(avail, 3).tolist(),
    "curt": curt.tolist(),
    "bess": {k: bess[k].tolist() for k in ["charge_kwh", "discharge_kwh", "aux_kwh", "available"]},
    "capTests": tests.to_dict("records"),
}
with open("site_data.json", "w") as fh:
    json.dump(out, fh, separators=(",", ":"))

print("load MWh", load.sum() / 1000, "peak", load.max())
print("solar kWh/kWdc", solar_unit.sum(), "POA kWh/m2", poa.sum() / 1000)
print("hybrid expected MWh", expected.sum() / 1000, "actual", actual.sum() / 1000)
