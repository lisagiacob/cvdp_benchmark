import json

file_A = "../dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial.jsonl"
file_B = "../example_dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl"
file_C = "../example_dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV1.jsonl"

# Leggo gli id di B
ids_B = set()
with open(file_B, "r") as f:
    for line in f:
        obj = json.loads(line)
        ids_B.add(obj["id"])

# Filtro A
with open(file_A, "r") as fA, open(file_C, "w") as fC:
    for line in fA:
        obj = json.loads(line)
        if obj["id"] not in ids_B:
            fC.write(line)

print("Fatto.")