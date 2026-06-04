"""Print pass/fail for key pipeline inputs under data/."""

from __future__ import annotations

from pathlib import Path

BASE = Path(__file__).resolve().parent

CHECKS: dict[str, list[tuple[str, bool]]] = {
    "02_01": [
        ("data/mapping/census bg to tract 2010/Census Tract Population_2010_short.csv", True),
        ("data/mapping/census bg to tract 2010/Census Block Group Population_2010_short.csv", True),
    ],
    "02_02": [
        ("data/mobility_data/EV_Toolbox/households_all_by_tract.csv", True),
        ("data/mobility_data/EV_Toolbox/evhouseholds_by_tract_acc2_2011.csv", True),
        ("data/mapping/census bg to tract 2010/popluation share bg to tract 2010.csv", True),
        ("data/mapping/census bg to tract 2010/map bg to TAZ 2010.csv", True),
        ("data/shps/taz_id.shp", False),
    ],
    "02_03": [
        ("data/mobility_data/CSTDM_processed/SDPTM_hh_home_TAZ.csv", True),
        ("data/mobility_data/EV_Toolbox/evhh_share_TAZ.csv", True),
    ],
    "02_04": [
        ("data/mobility_data/CSTDM/SDPTM/trips_1.csv", True),
        ("data/mobility_data/CSTDM/LDPTM/LDPTM_Trips.csv", True),
        ("data/mobility_data/CSTDM/LDPTM/LDPTM_AccEgr.csv", True),
        ("data/mobility_data/CSTDM/ETM/trips_Ext.csv", True),
        ("data/mobility_data/CSTDM_processed/EVhh_new_SDPTM_sample42.csv", True),
        ("data/mobility_data/TAZ_distance/parsed_100000_LDPTM", False),
        ("data/mobility_data/TAZ_distance/parsed_100000_ETM", False),
    ],
    "03_02": [
        ("data/mobility_data/TAZ_distance/parsed_100000_LDPTM", True),
        ("data/mobility_data/TAZ_distance/parsed_100000_ETM", True),
    ],
}


def main() -> None:
    for step, items in CHECKS.items():
        print(f"--- {step} ---")
        for rel, required in items:
            path = BASE / rel
            ok = path.exists()
            tag = "OK" if ok else ("MISSING" if required else "optional missing")
            extra = ""
            if ok and path.is_dir():
                extra = f" ({len(list(path.iterdir()))} files)"
            print(f"  [{tag}] {rel}{extra}")


if __name__ == "__main__":
    main()
