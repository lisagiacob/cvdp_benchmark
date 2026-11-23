# CVDP Claw Code Agent

This adapter lets CVDP run the sibling `claw-code` Rust CLI as an agentic benchmark worker.

## Build

From `src/agents/claw-code`:

```bash
CLAW_CODE_ROOT="../../../claw-code" ./build_agent.sh
```

`CLAW_CODE_ROOT` must point to the repository that contains `rust/Cargo.toml`.

## Run

Use the normal CVDP agentic path, selecting the Claw image:

```bash
export AGENT_BACKEND=claw
export ANTHROPIC_VERTEX_PROJECT_ID="my-gcp-project"
export ANTHROPIC_VERTEX_LOCATION="global"
export GOOGLE_ACCESS_TOKEN="$(gcloud auth application-default print-access-token)"
export LLM_MODEL="vertex_ai/claude-sonnet-4-6"

python run_benchmark.py -f example_dataset/single_datapoint.agentic_transformed.jsonl \
  -l -g claw-code-agent
```

Set `AGENT_BACKEND=opencode` to use the existing OpenCode wrapper.

## Runtime Variables

- `LLM_MODEL`: shared `<provider>/<model>` setting (see the main README); `vertex_ai/<model>` is passed to `claw --model` as `google-vertex-anthropic/<model>`. A bare name keeps the legacy rule: Claude models/aliases get the Vertex prefix when `ANTHROPIC_VERTEX_PROJECT_ID` is set.
- `CLAW_PERMISSION_MODE`: passed to `--permission-mode`; defaults to `danger-full-access`.
- `CLAW_SKIP_PERMISSIONS`: set to `1` to pass `--dangerously-skip-permissions`.
- `CLAW_TIMEOUT_SECONDS`: wrapper timeout; defaults to `600`.
- `ANTHROPIC_VERTEX_PROJECT_ID`, `ANTHROPIC_VERTEX_LOCATION`, `GOOGLE_ACCESS_TOKEN`: used for Vertex Anthropic.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`: forwarded into the container when present.

The wrapper logs to `/code/claw_stdout.log` and `/code/claw_stderr.log`.
