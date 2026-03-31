import json, pathlib
p = pathlib.Path(r"E:/GitHub/good_model/EV_Charging_Marginal_Emissions.ipynb")
nb = json.loads(p.read_text(encoding="utf-8"))
cells_to_dump = [0, 5, 7, 8, 9, 12, 13, 15, 16, 19, 22, 27, 39, 40, 43, 45, 56, 58, 59, 60]
for i in cells_to_dump:
    c = nb["cells"][i]
    s = "".join(c.get("source", []))
    print(f"===== CELL {i} =====")
    print(s)
    print()
