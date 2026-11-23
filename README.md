# Agentic Workflows for RTL Code Generation

Master's thesis project (Politecnico di Torino, A.Y. 2025/2026) by **Lisa Giacobazzi**.
Supervisors: Alessio Burrello, Daniele Jahier Pagliari.

This repository extends NVIDIA's [CVDP benchmark](https://github.com/NVlabs/cvdp_benchmark) (Comprehensive Verilog Design Problems, v1.0.2) to evaluate modern **AI coding agents** on RTL design tasks. The work has two goals: measuring how well the agents perform, and understanding **why** they fail.

The evaluation covers the **160 code-generation datapoints** of CVDP (categories cid003, cid004, cid005, cid012, cid013, cid014, cid016). 92 of them run on open-source tools (Icarus/cocotb). The other 68 need commercial EDA tools and run on Cadence Xcelium.

The thesis runs use CVDP **v1.0.2**. The repository also supports **v1.1.0**, where the same tasks come with a harness migrated to cocotb 2, and can re-grade the v1.0.2 runs with it without re-running the agents (see [section 5](#5-cvdp-v110-and-harness-re-runs)).

Main additions to the original CVDP infrastructure:

- **Non-agentic LLM wrappers** for Claude, GPT and Gemini models, built on the standard CVDP pipeline ([src/llm_models/](src/llm_models/)).
- **Agentic integrations**, each running in its own Docker container on a shared workspace: [OpenCode](src/agents/opencode/), [Claude Code](src/agents/ClaudeCode/), [Claw Code](src/agents/claw-code/), [Codex](src/agents/codex/).
- **Cadence Xcelium verification image** for the commercial datapoints ([docker/Dockerfile-cadence-verif](docker/Dockerfile-cadence-verif)).
- **Log mining and failure analysis** of every failed agentic run ([log_mining_agentic.py](log_mining_agentic.py), [log_mining.py](log_mining.py)).

Changes to the behaviour of the original CVDP pipeline:

- **Expected file names in `--force-agentic` prompts** ([src/data_transformer.py](src/data_transformer.py)). When a non-agentic datapoint is converted to agentic format, the sentence `Write your solution to the file: <path>` (or `to these files: <paths>` when there are several) is appended to its prompt. Code comprehension categories are excluded. In Copilot mode the model is told where its answer will be saved, but an agent writes the files itself, and without the expected paths it picked file names that the harness did not compile, so the datapoint failed regardless of the RTL quality.

For details on the original benchmark, see [README_CVDP.md](README_CVDP.md), [README_AGENTIC.md](README_AGENTIC.md), [README_NON_AGENTIC.md](README_NON_AGENTIC.md) and the [CVDP paper](https://arxiv.org/abs/2506.14074).

---

## Repository layout

| Path | Content |
|---|---|
| `run_benchmark.py`, `run_samples.py`, `run_reporter.py` | CVDP entry points (single run, multi-sample pass@k, report regeneration) |
| `dataset/` | CVDP v1.0.2 datasets used in the thesis (including cocotb v1/v2 variants) and the v1.1.0 agentic datasets (cocotb 2) |
| `tools/resume_agentic_eval.py` | Resumes an interrupted agentic run, or re-grades a finished one with a newer dataset harness |
| `src/` | Benchmark core, LLM wrappers (`llm_models/`) and agent adapters (`agents/`) |
| `docker/` | Verification images: open-source (`Dockerfile.osvb-cocotb`) and Cadence (`Dockerfile-cadence-verif`) |
| `Makefile` | Shortcuts to build images and launch every thesis experiment |
| `log_mining*.py` | Failure analysis scripts |

---

## How to run

### 1. Benchmark setup (official CVDP instructions)

Prerequisites, installation, `.env` configuration, golden-solution sanity check and non-agentic runs follow the **official CVDP instructions**. Use them as they are:

- [README_CVDP.md](README_CVDP.md) — Quick Start (Python 3.12, Docker CE, virtual environment, `requirements.txt`, `.env`)
- [README_NON_AGENTIC.md](README_NON_AGENTIC.md) and [README_AGENTIC.md](README_AGENTIC.md) — the two evaluation workflows
- Upstream repository: [NVlabs/cvdp_benchmark](https://github.com/NVlabs/cvdp_benchmark)

Complete that setup before going on. The steps below cover only what this thesis adds on top of it.

### 2. Thesis-specific configuration

- **Folder name.** The Makefile targets call `../cvdp_benchmark/run_benchmark.py`, so clone this repository into a folder named `cvdp_benchmark` (`git clone https://github.com/lisagiacob/master_tesis_cvdp.git cvdp_benchmark`), or update the paths in the `Makefile`.
- **`.env`.** Create it with `cp .env.example .env` (or `make setup`) and fill in the variables for the providers and agents you want to use (see below). [.env.example](.env.example) lists all of them.
- **Machine-specific paths.** ⚠️ The Vertex AI credential mounts are hard-coded for the thesis server in `_gcp_auth_volumes()` and `_generate_gcp_host_access_token()` in [src/dataset_processor.py](src/dataset_processor.py) (`/home/giacobazzi/...`, `/snap/bin/gcloud`). The same applies to the Cadence install path in [docker/Dockerfile-cadence-verif](docker/Dockerfile-cadence-verif) and [src/config_manager.py](src/config_manager.py). Adapt them to your machine before running agentic or commercial experiments.

### 3. LLM providers

The thesis experiments used **Claude Sonnet 4.6** (and Claude 3.7 Sonnet for the model-scaling comparison). Each provider is configured through `.env`:

| Provider | `.env` variables | Used by |
|---|---|---|
| **Google Vertex AI** (Claude) | `ANTHROPIC_VERTEX_PROJECT_ID`, `ANTHROPIC_VERTEX_LOCATION` (e.g. `global`), `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES=1` | Non-agentic `claude-sonnet-4-6`, Claude Code, OpenCode, Claw Code (`vertex_ai/...`) |
| **Anthropic API** | `ANTHROPIC_API_KEY` (optionally `ANTHROPIC_BASE_URL`) | Claude Code and Claw Code (`LLM_MODEL=anthropic/...`, or a bare name when no Vertex project is set) |
| **AWS Bedrock** | `BEDROCK_AWS_ACCESS_KEY_ID`, `BEDROCK_AWS_SECRET_ACCESS_KEY`, `BEDROCK_AWS_REGION_NAME` | Claude Code, OpenCode (`LLM_MODEL=bedrock/...`). With a `bedrock/` model no Vertex credential is forwarded to the agent container |
| **OpenRouter** | `OPENROUTER_API_KEY` | Non-agentic `claude-3.7-sonnet`; OpenCode with `claude-3.7-*` or `gemini-*` models |
| **OpenAI** | `OPENAI_USER_KEY` (non-agentic), `OPENAI_API_KEY` (agents) | Non-agentic `gpt-*` / `o3-pro` (official factory); OpenCode with `gpt-*` models |
| **Google Gemini API** | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | Non-agentic `gemini`, `gemini-2.5-flash`, `gemini-2.5-pro` |

For Vertex AI, authenticate on the host with `gcloud auth application-default login`. Agent containers receive a fresh access token generated on the host, plus the mounted gcloud config.

**Choosing the model:**

| Workflow | How the model is selected |
|---|---|
| Non-agentic | `-m <model>` (or `MODEL=<model>` with `make`). Claude, Gemini and OpenRouter models need the custom factory: `-c src/llm_models/custom_model_factory.py` or `CUSTOM_MODEL_FACTORY=src/llm_models/custom_model_factory.py` in `.env` |
| All agents | `LLM_MODEL=<provider>/<model>` (LiteLLM form), shared by Claude Code, OpenCode, Claw Code and Codex. Change it with `make change-model MODEL=<provider>/<model>` |
| Claude Code | `CLAUDE_CODE_MAX_TURNS` (default `30`) |

Each agent translates `LLM_MODEL` to its own naming scheme:

| `LLM_MODEL` | Claude Code | OpenCode | Claw Code |
|---|---|---|---|
| `bedrock/<id>` (e.g. `bedrock/global.anthropic.claude-sonnet-4-6`) | Bedrock, model `<id>` | `amazon-bedrock/<id>` | not supported |
| `vertex_ai/<id>` (e.g. `vertex_ai/claude-sonnet-4-6@default`) | Vertex, model `<id>` | `google-vertex-anthropic/<id>` | `google-vertex-anthropic/<id>` |
| `anthropic/<id>` | direct API, model `<id>` | `anthropic/<id>` | `<id>` |
| `openrouter/<id>`, `openai/<id>` | — | as is | as is |
| bare name (legacy, e.g. `claude-sonnet-4-6`) | Vertex if a project is set, else direct API | `claude-*` → Vertex, `claude-3.7-*` / `gemini-*` → OpenRouter, `gpt-*` → OpenAI | Vertex prefix if a project is set |

Default when unset: `claude-sonnet-4-6` (bare name).

Other useful variables: `TEMPERATURE` (default `0.0`), `CLAUDE_MAX_TOKENS` (non-agentic Claude output limit).

### 4. Running the experiments (Makefile)

Every thesis experiment has a `make` target. Run the commands from the repository root.

**Build the images first:**

| Target | What it builds |
|---|---|
| `make cocov1` / `make cocov2` | Open-source simulation images `osvb-cocotb:v1` (cocotb 1.9.2) and `osvb-cocotb:v2` (cocotb 2.0.1). Both can coexist: the `*-cocov1` / `*-cocov2` targets pick the matching one by overriding `OSS_SIM_IMAGE` |
| `make cadence-up` | Cadence Xcelium image `cvdp-cadence-verif:latest` (commercial datasets only) |
| `make cadence-cocotb2` | `cvdp-cadence-verif:cocotb2`: the same image with cocotb 2.0.1, for the v1.1.0 commercial dataset (run `make cadence-up` first) |
| `make claude-up` | Claude Code agent images `claude-code-agent:v1` / `:v2` |
| `make opencode-up` | OpenCode agent images `cvdp-opencode-agent:v1` / `:v2` (built from `../opencode`) |
| `make codex-up` | Codex agent images `codex-agent:v1` / `:v2` (pin the CLI with `CODEX_VERSION=x.y.z`) |

Each agent build script ([src/agents/](src/agents/)`*/build_agent.sh`) builds one `cvdp-agent-base` + agent image per cocotb version, so the agent tests with the same cocotb as the verification image. Build a single version with `COCOTB_VERSIONS=v2`. The v2 images are also tagged `:latest`, and each `cvdp-<agent>` image gets an alias without the `cvdp-` prefix (e.g. `opencode-agent:v2`); aliases share the same image ID and take no extra space.

**Then launch a benchmark:**

| Experiment | Claude Code | OpenCode | Codex |
|---|---|---|---|
| Agentic, non-commercial, cocotb v1 | `make claude-cocov1` | `make opencode-cocov1` | `make codex-cocov1` |
| Agentic, non-commercial, cocotb v2 | `make claude-cocov2` | `make opencode-cocov2` | `make codex-cocov2` |
| Agentic, commercial (Cadence) | `make claude-commercial` | `make opencode-commercial` | `make codex-commercial` |
| Single datapoint (debug, cocotb v2) | `make claude-sd DP=<id>` | `make opencode-sd DP=<id>` | `make codex-sd DP=<id>` |

The `*-commercial` targets pass the agent image without a tag, so they use `:latest` (= v2). Targets that don't override `OSS_SIM_IMAGE` use the value in `.env`.

| Experiment | Target |
|---|---|
| Non-agentic code generation, cocotb v1 / v2 | `make nonagentic-codegen-v1 MODEL=<model>` / `make nonagentic-codegen-v2 MODEL=<model>` |

Make variables:
- `MODEL=<model>`: model for the non-agentic targets. Always set it: the default `azure-gpt-4o` is not registered in this repository's model factories.
- `DP=<datapoint id>`: datapoint for the `*-sd` targets (default `cvdp_agentic_AES_encryption_decryption_0018`).

Every `*-sd`, `*-cocov*`, `*-commercial` and `nonagentic-*` target:
- writes results to its own `work_<target>/` directory (`report.txt`, `report.json`, plus per-datapoint harness and agent logs), so different targets can run in parallel; override it with `BENCHMARK_PREFIX=<dir>`;
- refuses to start if that directory is not empty (move or delete previous results first);
- sends an email with the outcome and duration when `NOTIFY_EMAIL_TO` is set in `.env`.

**Claw Code** has no Makefile target. Build and run it directly (see [src/agents/claw-code/README.md](src/agents/claw-code/README.md)):

```bash
CLAW_CODE_ROOT=../claw-code src/agents/claw-code/build_agent.sh
./run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl -l -g claw-code-agent
```

The Makefile targets are shortcuts for `run_benchmark.py`. For example, `make claude-sd DP=<id>` is equivalent to:

```bash
./run_benchmark.py -f dataset/cvdp_v1.0.2_agentic_code_generation_no_commercial_COCOTBV2.jsonl \
  -l -g claude-code-agent:v2 -i <id>
```

The agent backend is chosen from the image name passed with `-g` (`codex`, `claw`, `claude`, otherwise OpenCode).

- OpenCode and Claw Code are built from source. Their repositories must be cloned next to this one (`../opencode`, `../claw-code`), or you can point to them with `CLAW_CODE_ROOT`.

### 5. CVDP v1.1.0 and harness re-runs

**What changes in v1.1.0.** The tasks are the same as in v1.0.2: same 160 ids, identical prompts, context files and reference patches. Only the harness changes, migrated to the cocotb 2 API:

- `.value.to_unsigned()` → `int(...)`, `units=` → `unit=`, `cocotb.runner` → `cocotb_tools.runner`, `TestFailure` → `AssertionError`;
- `period_high` on clocks with an odd period;
- `defines={"SIMULATION": None}` → `1`;
- no more `cocotb` / `cocotb-bus` version pins in the harness Dockerfiles.

The non-commercial datapoints are in a single file, with no cocotb V1/V2 split: all 92 run on `osvb-cocotb:v2`.

| File | Datapoints |
|---|---|
| `dataset/cvdp_v1.1.0_agentic_code_generation_no_commercial.jsonl` | 92 (open-source tools, cocotb 2) |
| `dataset/cvdp_v1.1.0_agentic_code_generation_commercial.jsonl` | 68 (Cadence Xcelium, cocotb 2) |

The migration fixes the failures caused by cocotb 2 incompatibilities in the v1.0.2 harness (e.g. `'Logic' object has no attribute 'to_unsigned'`, `Bad period`). Dataset design flaws such as a `.env` that leaves out RTL files, undocumented internal signals the testbench reads, or specs and testbenches that disagree are still there.

Two infrastructure settings are needed for the commercial v1.1.0 harness:

- **`cvdp-cadence-verif:cocotb2`** (`make cadence-cocotb2`). The harness Dockerfiles install `cocotb==2.0.1`, which the `cocotb<2.0` constraint of `cvdp-cadence-verif:latest` rejects.
- **`NEW_HARNESS=true`.** The cocotb 2 runner passes `-timescale` to xrun itself. Without the flag, the default timescale that [src/commercial_eda.py](src/commercial_eda.py) adds through `XRUNOPTS` would be a second one, and xrun stops with `*E,OPTNOML`. With the flag, `XRUNOPTS` is left out only for the services whose cocotb runner sets a timescale. Leave it unset for v1.0.2.

**Running on v1.1.0.** The `*-v110*` targets set `OSS_SIM_IMAGE=osvb-cocotb:v2`, `VERIF_EDA_IMAGE=cvdp-cadence-verif:cocotb2` and `NEW_HARNESS=true`, and use the `:v2` agent images:

| Experiment | Claude Code | OpenCode | Codex |
|---|---|---|---|
| Agentic, non-commercial | `make claude-v110` | `make opencode-v110` | `make codex-v110` |
| Agentic, non-commercial, harness aware (BA) | `make claude-v110-ba` | `make opencode-v110-ba` | `make codex-v110-ba` |
| Agentic, commercial | `make claude-v110-commercial` | `make opencode-v110-commercial` | `make codex-v110-commercial` |
| Agentic, commercial, xrun in the agent container | `make claude-v110-commercial-eda` | `make opencode-v110-commercial-eda` | `make codex-v110-commercial-eda` |
| Agentic, commercial, harness aware (BA) | `make claude-v110-commercial-ba` | `make opencode-v110-commercial-ba` | `make codex-v110-commercial-ba` |

They behave like the v1.0.2 targets: they write to `work_<target>/`, refuse to start on a non-empty directory and send the notification email.

**Re-grading a v1.0.2 run with the v1.1.0 harness.** The agent inputs are the same in both versions, so the RTL an agent produced on v1.0.2 can be graded again with the v1.1.0 harness, without running the agent again:

```bash
make rerun-harness WORK=work_opencode_cocov2-ba_5-5          # -> work_opencode_cocov2-ba_5-5_rr/
make rerun-harness WORK=work_opencode_commercial-ba_5-5 DRY=1  # only print what would run
```

The target calls [tools/resume_agentic_eval.py](tools/resume_agentic_eval.py) with `--copy-to --only-eval --refresh-harness`:

1. **Copy.** `WORK` is copied to `<WORK>_rr` (override with `RR_OUT=<dir>`), and only the copy is touched. The original keeps its results, logs and `raw_result.json`.
2. **Refresh.** For each datapoint the agent processed, `src/`, `docker-compose.yml` and `Dockerfile` are rewritten from the v1.1.0 dataset. The previous files are moved to `harness/<n>/harness_backup_<timestamp>/`, and the agent output (`rtl/`, `verif/`, `docs/`, `agent_changes.patch`) is left as it is.
3. **Images.** Image placeholders resolve to the images of the original run, except `__OSS_SIM_IMAGE__` → `osvb-cocotb:v2` and, on commercial runs, `__VERIF_EDA_IMAGE__` → `cvdp-cadence-verif:cocotb2` (with `--new-harness`).
4. **Evaluation and report.** The harness runs again. The external Docker network of the original run is recreated if it's gone. The datapoints of the dataset that the run doesn't contain (e.g. the V2 ones for a V1 run) are skipped. At the end the report is regenerated in `<WORK>_rr/report.txt`, and the full log goes to `<WORK>_rr.log`.

The dataset is picked from the name: `commercial` if `WORK` contains it, `no_commercial` otherwise (override with `DATASET=commercial|no_commercial`). The agent image is read from the run (override with `AGENT=<image>`); with `--only-eval` it is only used to label the report.

The same script without `--refresh-harness` resumes an interrupted run: see the header of [tools/resume_agentic_eval.py](tools/resume_agentic_eval.py) for all its options.

### 6. Failure analysis

```bash
# Agentic runs: per-run loop outcome + reason the datapoint failed
python3 log_mining_agentic.py work --report work/report.txt --out <output-dir>

# Non-agentic runs: harness error classification
python3 log_mining.py work/report.txt
```

`log_mining_agentic.py` writes `agent_runs.csv`, `agent_failure_reasons.csv` and `agent_summary.txt`, including token usage and cost.

---

## License

Based on the NVIDIA CVDP benchmark, released under the Apache 2.0 license. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
