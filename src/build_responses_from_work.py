import os
import json
from pathlib import Path

WORK_DIR = "work"
OUTPUT_FILE = "work/responses.jsonl"

def extract_index(filename):
    # "6.md.raw_response.txt" → 6
    return int(filename.split(".")[0])

results = []

for base_dir in Path(WORK_DIR).iterdir():
    if not base_dir.is_dir():
        continue

    base_name = base_dir.name  # es: cvdp_copilot_16qam_mapper
    prompts_dir = base_dir / "prompts"

    if not prompts_dir.exists():
        continue

    for file in prompts_dir.glob("*.md.raw_response.txt"):
        idx = extract_index(file.name)

        # ricostruisci ID con padding
        full_id = f"{base_name}_{idx:04d}"

        with open(file, "r") as f:
            content = f.read().strip()

        results.append({
            "id": full_id,
            "completion": content
        })

# salva
with open(OUTPUT_FILE, "w") as out:
    for r in results:
        out.write(json.dumps(r) + "\n")

print(f"Creato {OUTPUT_FILE} con {len(results)} risposte")