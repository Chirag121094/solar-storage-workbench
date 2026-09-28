# Solar + Storage Workbench

Two working engineering tools for commercial solar and battery storage, built by **Chirag Shetty**, renewable energy and BESS engineer.

**Live site:** https://chirag121094.github.io/solar-storage-workbench/  
**Contact:** [LinkedIn](https://www.linkedin.com/in/cs753951/) · chiragshetty68@gmail.com

![NEM 3.0 storage savings](docs/nem-savings.png)

## 1. NEM 3.0 storage savings

California's Net Billing Tariff (NEM 3.0) credits exported solar at hourly Avoided Cost Calculator (ACC) values instead of the retail rate. Midday exports now earn a few cents per kWh. This tool shows how much of the solar business case that removes, and how much a battery wins back.

It bills the same site four ways on the same TOU tariff:

| Scenario | What changes |
|---|---|
| A. Baseline | No solar |
| B. Solar, NEM 2.0 | Exports earn the retail TOU rate minus non-bypassable charges |
| C. Solar, NEM 3.0 | Exports earn the hourly ACC value |
| D. Solar + BESS, NEM 3.0 | Battery stores midday surplus and serves the 4–9 pm peak |

**Inputs.** Each is a CSV with a timestamp column and a kWh column, 8760 hourly or 35040 15-minute rows:
1. Utility load interval data (required)
2. Solar production 8760 (optional; a modeled profile is used otherwise)
3. Energy Toolbase battery export (optional; one signed column, or separate discharge and charge columns)

Tariff rates, the 12 × 24 ACC export matrix and project costs are editable on the page.

**Sample result** (synthetic 3.6 GWh/yr warehouse, 1,500 kWdc PV, 500 kW / 2,000 kWh BESS, illustrative rates):

| | Annual bill | Savings |
|---|---|---|
| Baseline | $1,010,696 | — |
| Solar, NEM 2.0 | $578,372 | $432,324 |
| Solar, NEM 3.0 | $634,857 | $375,839 |
| Solar + BESS, NEM 3.0 | $466,127 | $544,569 |

The move to NEM 3.0 costs this site about $56k a year. The battery recovers $169k a year over solar alone, most of it from demand charges.

## 2. Hybrid asset performance

![Hybrid asset performance](docs/asset-performance.png)

For a PV + BESS plant with hourly SCADA:
- Expected output from a PVWatts-style model (Sandia module temperature, −0.35 %/°C, DC losses, inverter efficiency, clipping)
- Performance ratio and weather-corrected performance index
- Loss attribution: availability, curtailment, soiling, other
- Daily soiling ratio with automatic detection of rain and wash events
- ASTM E2848 regression capacity test on any 14-day window
- Battery round-trip efficiency with and without auxiliary load, availability, cycles and state of health

## Run the Python engines

The website runs the same calculations in the browser. The Python and browser results match to the dollar on the sample data.

```bash
pip install -r requirements.txt
cd python

# NEM savings: one combined file, or separate load / solar / battery files
python nem_bess_savings.py ../data/sample_site_8760.csv
python nem_bess_savings.py --load utility.csv --solar solar_8760.csv --battery etb_export.csv

# Asset performance
python asset_performance.py ../data/hybrid_site_scada_8760.csv ../data/bess_daily.csv ../data/bess_capacity_tests.csv

# Regenerate the synthetic sample data
python gen_data.py
```

## Repository layout

```
index.html        the website (single file, no build step)
data/             synthetic sample CSVs and the ACC matrix template
python/           nem_bess_savings.py, asset_performance.py, gen_data.py
docs/             screenshots
```

## Publish on GitHub Pages

1. Create a public repository named `solar-storage-workbench` and upload these files.
2. Settings → Pages → Source: **Deploy from a branch**, Branch: **main**, folder **/ (root)**.
3. The site appears at `https://chirag121094.github.io/solar-storage-workbench/` within a minute or two.
4. Optional: add a custom domain under Settings → Pages.

## Data and assumptions

All sites, SCADA data and rates are synthetic or illustrative. No client data is used. The tariff follows the structure of a PG&E B-19 secondary TOU schedule, and the ACC matrix follows the typical shape of published values. Neither uses current published numbers. Replace both with the current tariff sheet and the customer's ACC vintage before using results for a real project.
