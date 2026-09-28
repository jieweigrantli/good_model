"""
08_11_fetch_pge_ica.py

Download PG&E's Integration Capacity Analysis (ICA) detailed results, the
numeric per-feeder hosting-capacity data behind the Grid Resource Integration
Portal at https://grip.pge.com.

Why this is needed
------------------
The transformer limit in the nested model is built in ``08_06`` from GRIP's
``DFSubstationArea___PeakFacilityLoadingPercent`` shapefile, summing the
``facilityra`` (MVA) of each listed bank per substation. That layer is a Grid
Needs Assessment product keyed on ``gnaneedid``: it lists the banks with an
identified need, not a substation's full asset inventory. The understatement is
severe where it bites. Substation 02201, SF X (MISSION), publishes exactly two
banks -- named "MISSION X BANK 9" and "MISSION X BANK 10", so banks 1-8 exist
and are simply absent -- summing to 9.88 MVA, while PG&E's own
``SubstationLoadProfile`` reports a measured peak of 118.9 MW at the same
substation id and name. The model then caps net import at 9.88 MW and sheds
53 GWh over four weeks at a substation the real grid serves without difficulty;
that single node was 41% of all S0 shortfall.

The ICA files carry ``IC_Safety_Bank_kW`` per line section, which for this
substation runs 18,240-46,090 kW -- consistent with the measured load and an
order of magnitude above the GNA sum.

How the download works
----------------------
The old ICA map download is retired and everything moved behind the GRIP
portal, which is an ArcGIS Hub site whose download widget does not expose
static links. The files are reachable anyway: PG&E publishes a feature service
whose single row holds presigned S3 URLs for every division, refreshed
periodically.

    PresignedURLPROD/FeatureServer/0  ->  one row, columns
      <division>url        per-division tabular ICA (zip of per-feeder .7z)
      drpcomplianceshpurl  the GRIP_SHP bundle already in data/GRIP_SHP
      drpcompliancefgdburl the same as a file geodatabase

Each division zip contains ``GICA_<feederid>.7z``, each holding one CSV with
576 rows per line section: 12 months x 24 hours x 2 loading scenarios (90th and
10th percentile) x load/generation. The feeder id begins with the five-digit
substation id, so feeders roll up to substations without a separate join.

Columns vary slightly between feeders (some carry ``Hourly_Load_ICA`` where
others carry ``Hourly_Load``), and redacted cells appear as the string
"REDACTED" rather than empty, so every numeric column needs coercing.

Usage
  python 08_11_fetch_pge_ica.py --list
  python 08_11_fetch_pge_ica.py --divisions "San Francisco" Mission
  python 08_11_fetch_pge_ica.py --all

Writes data/ica/pge/<Division>.zip and presigned_urls.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

PKG = Path(__file__).resolve().parent
sys.path.insert(0, str(PKG))

import common as C

OUT_DIR = C.DATA_DIR / "ica" / "pge"
PRESIGNED = (
    "https://services2.arcgis.com/mJaJSax0KPHoCNB6/arcgis/rest/services"
    "/PresignedURLPROD/FeatureServer/0/query?where=1%3D1&outFields=*&f=json"
)
UA = {"User-Agent": "Mozilla/5.0"}

# column name -> division name, for the per-division tabular downloads
DIVISION_FIELDS = {
    "centralcoasturl": "Central Coast", "deanzaurl": "De Anza",
    "diablourl": "Diablo", "eastbayurl": "East Bay", "fresnourl": "Fresno",
    "humboldturl": "Humboldt", "kernurl": "Kern", "lospadresurl": "Los Padres",
    "missionurl": "Mission", "northbayurl": "North Bay",
    "northvalleyurl": "North Valley", "peninsulaurl": "Peninsula",
    "sacramentourl": "Sacramento", "sanfranciscourl": "San Francisco",
    "sanjoseurl": "San Jose", "sierraurl": "Sierra", "sonomaurl": "Sonoma",
    "stocktonurl": "Stockton", "yosemiteurl": "Yosemite",
}


def fetch_presigned() -> dict:
    """The single row of presigned URLs. They expire, so fetch immediately before use."""
    req = urllib.request.Request(PRESIGNED, headers=UA)
    blob = json.loads(urllib.request.urlopen(req, timeout=120).read())
    feats = blob.get("features") or []
    if not feats:
        raise SystemExit(f"No rows returned from {PRESIGNED}")
    return feats[0]["attributes"]


def download(url: str, dest: Path) -> None:
    C.ensure_dir(dest.parent)
    t0 = time.time()
    req = urllib.request.Request(url, headers=UA)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=1800) as r, open(tmp, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)
    mb = dest.stat().st_size / 1e6
    print(f"  {dest.name}: {mb:,.1f} MB in {time.time() - t0:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="Show divisions and exit.")
    ap.add_argument("--divisions", nargs="*", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--skip-existing", action="store_true", default=True)
    ap.add_argument("--force", dest="skip_existing", action="store_false")
    args = ap.parse_args()

    if args.list:
        print("PG&E divisions:")
        for name in sorted(DIVISION_FIELDS.values()):
            print(f"   {name}")
        return

    att = fetch_presigned()
    C.ensure_dir(OUT_DIR)
    (OUT_DIR / "presigned_urls.json").write_text(json.dumps(att, indent=1), encoding="utf-8")

    wanted = set(DIVISION_FIELDS.values()) if args.all else set(args.divisions or [])
    if not wanted:
        raise SystemExit("Pass --all, or --divisions NAME [NAME ...]; --list shows the names.")
    unknown = wanted - set(DIVISION_FIELDS.values())
    if unknown:
        raise SystemExit(f"Unknown divisions: {sorted(unknown)}")

    print(f"Downloading {len(wanted)} division(s) to {OUT_DIR}")
    for field, name in DIVISION_FIELDS.items():
        if name not in wanted:
            continue
        dest = OUT_DIR / f"{name}.zip"
        if args.skip_existing and dest.is_file():
            print(f"  {dest.name}: exists, skipping")
            continue
        url = att.get(field)
        if not url:
            print(f"  {name}: no URL in the presigned row")
            continue
        download(url, dest)

    print(f"\nDone. Next: aggregate IC_Safety_Bank_kW per substation to replace the "
          f"GNA-derived ratings in 08_06.")


if __name__ == "__main__":
    main()
