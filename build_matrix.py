import json
import re
from pathlib import Path

source = Path("data/breaks_overview_dom.json")
output = Path("data/hazard_hunt_november_matrix.json")

data = json.loads(source.read_text())
elements = data["elements"]

headers = [
    e["text"].strip()
    for e in elements
    if e["tag"] == "TH"
]

cells = [
    e["text"].strip()
    for e in elements
    if e["tag"] == "TD"
]

# Table layout:
# SCENARIO | 23 models | BROKEN
column_headers = headers[:25]
scenario_names = headers[25:-1]
model_total_label = headers[-1]

models = column_headers[1:24]

assert column_headers[0] == "SCENARIO"
assert column_headers[-1] == "BROKEN"
assert len(models) == 23, f"Expected 23 models, got {len(models)}"
assert len(scenario_names) == 41, (
    f"Expected 41 scenarios, got {len(scenario_names)}"
)
assert model_total_label == "MODEL TOTAL"
assert len(cells) == 42 * 24, (
    f"Expected 1008 cells, got {len(cells)}"
)

rows = [
    cells[i:i + 24]
    for i in range(0, len(cells), 24)
]

scenarios = []

for name, row in zip(scenario_names, rows[:41]):
    model_statuses = {
        model: status
        for model, status in zip(models, row[:23])
    }

    match = re.fullmatch(r"(\d+)/23", row[23])

    scenarios.append({
        "name": name,
        "models": model_statuses,
        "broken": int(match.group(1)) if match else None,
        "broken_display": row[23],
    })

model_totals = {
    model: status
    for model, status in zip(models, rows[41][:23])
}

result = {
    "event": "Hazard Hunt November '26",
    "source_url": data["url"],
    "source_title": data["title"],
    "scenario_count": len(scenarios),
    "model_count": len(models),
    "combination_count": len(scenarios) * len(models),
    "models": models,
    "scenarios": scenarios,
    "model_totals": model_totals,
    "overall_total": rows[41][23],
    "source": "captured rendered Breaks Overview DOM",
}

output.write_text(json.dumps(result, indent=2, ensure_ascii=False))

print("Matrix generated successfully.")
print("Scenarios:", len(scenarios))
print("Models:", len(models))
print("Combinations:", len(scenarios) * len(models))
print("Model totals:", model_totals)
print("Overall total:", result["overall_total"])
print("Saved:", output)
