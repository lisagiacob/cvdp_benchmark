#!/usr/bin/env python3

"""
CVDP agent that runs the OpenAI Codex CLI (`codex exec`) on RTL benchmark tasks.

Expected flow:
1. CVDP mounts the datapoint workspace at /code and writes /code/prompt.json.
2. This script reads the task from /code/prompt.json.
3. `codex exec --json` operates directly on /code with its native tools.
4. CVDP runs its normal validation afterward.

Model / provider — chosen by the provider prefix of LLM_MODEL:
- openrouter/<id> -> OpenRouter Responses API with OPENROUTER_API_KEY.
- openai/<id>     -> OpenAI API with OPENAI_API_KEY.
- bedrock/<id>, vertex_ai/<id> -> a Responses-compatible proxy (e.g. LiteLLM)
  at CODEX_PROXY_BASE_URL, which receives the full LLM_MODEL as model name.
  Codex only speaks the Responses wire API, which neither Bedrock nor Vertex
  expose for every model, hence the proxy.
- bare gpt-*/o* name -> OpenAI API (legacy shorthand).

The final "[result] ..." line uses the same format as claude-code-agent.py so
log_mining_agentic.py parses both, plus a trailing "duration=<s>s" (wall-clock
time of `codex exec`). Codex reports tokens but not cost: the cost is computed
from CODEX_PRICE_INPUT / CODEX_PRICE_CACHED / CODEX_PRICE_OUTPUT (USD per 1M
tokens). When they are unset and the provider is OpenRouter, the prices come
from OpenRouter's public model list; otherwise the cost is 0.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.cvdp_prompt import build_cvdp_prompt
from common.llm_model import split_llm_model

CODE_ROOT = Path("/code")
PROMPT_JSON_PATH = CODE_ROOT / "prompt.json"
EVENTS_LOG_PATH = CODE_ROOT / "rundir" / "codex_events.jsonl"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", "/opt/codex-home"))
LLM_PROVIDER, MODEL = split_llm_model()
# Same default as the OpenCode agent's process timeout.
TIMEOUT_SECONDS = int(os.environ.get("CODEX_TIMEOUT_SECONDS", "").strip() or "600")
REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "").strip()

TAG = "[codex-agent.py]"


# ---------------------------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------------------------

def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def resolve_provider() -> tuple[str, dict | None, str]:
    """
    Map LLM_MODEL onto Codex's config. Returns (provider_id, provider_table,
    model): provider_table is None for Codex's built-in `openai` provider,
    otherwise a [model_providers.<id>] table to write into config.toml.
    """
    if LLM_PROVIDER == "openrouter":
        return "openrouter", {
            "name": "OpenRouter",
            "base_url": "https://openrouter.ai/api/v1",
            "env_key": "OPENROUTER_API_KEY",
            "wire_api": "responses",
        }, MODEL

    if LLM_PROVIDER in ("bedrock", "vertex_ai"):
        base_url = _env("CODEX_PROXY_BASE_URL")
        if not base_url:
            raise SystemExit(
                f"{TAG} LLM_MODEL={LLM_PROVIDER}/{MODEL} needs a Responses-compatible "
                "proxy (e.g. LiteLLM): set CODEX_PROXY_BASE_URL in .env."
            )
        table = {
            "name": "CVDP proxy",
            "base_url": base_url,
            "wire_api": "responses",
        }
        if _env("CODEX_PROXY_API_KEY"):
            table["env_key"] = "CODEX_PROXY_API_KEY"
        # LiteLLM routes on the "<provider>/<model>" name, so pass it whole.
        return "cvdp-proxy", table, f"{LLM_PROVIDER}/{MODEL}"

    if LLM_PROVIDER == "openai" or (
        not LLM_PROVIDER and MODEL.lower().startswith(("gpt", "o1", "o3", "o4", "codex"))
    ):
        # `codex exec` authenticates the built-in openai provider via CODEX_API_KEY.
        if _env("OPENAI_API_KEY") and not _env("CODEX_API_KEY"):
            os.environ["CODEX_API_KEY"] = _env("OPENAI_API_KEY")
        return "openai", None, MODEL

    raise SystemExit(
        f"{TAG} Unsupported LLM_MODEL for Codex: {os.environ.get('LLM_MODEL', '')!r}. "
        "Use openrouter/, openai/, bedrock/ or vertex_ai/."
    )


def _toml_value(value) -> str:
    # json.dumps gives a valid TOML basic string for str values.
    return json.dumps(value)


def write_codex_config(provider_id: str, table: dict | None, model: str) -> None:
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    lines = [
        f"model = {_toml_value(model)}",
        f"model_provider = {_toml_value(provider_id)}",
        # No web access: the agent must solve the task from the workspace.
        'web_search = "disabled"',
    ]
    if REASONING_EFFORT:
        lines.append(f"model_reasoning_effort = {_toml_value(REASONING_EFFORT)}")
    if table:
        lines += ["", f"[model_providers.{provider_id}]"]
        lines += [f"{key} = {_toml_value(value)}" for key, value in table.items()]

    config_path = CODEX_HOME / "config.toml"
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{TAG} Wrote Codex config to {config_path}:")
    for line in lines:
        print(f"{TAG}   {line}")


# ---------------------------------------------------------------------------
# Prompt / payload helpers
# ---------------------------------------------------------------------------

def read_prompt_payload() -> dict:
    try:
        with open(PROMPT_JSON_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
    except Exception as e:
        print(f"{TAG} Error reading prompt.json: {e}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Event stream handling
# ---------------------------------------------------------------------------

def _new_usage_totals() -> dict:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "tool_calls": 0,
        "failed": False,
    }


def _short(text, limit: int = 160) -> str:
    return str(text).replace("\n", " ")[:limit]


def _log_event(event: dict, totals: dict) -> None:
    etype = event.get("type")
    item = event.get("item") if isinstance(event.get("item"), dict) else {}
    itype = item.get("type")

    if etype == "item.completed":
        # Tool lines use claude-code-agent.py's "[tool_use] Name(args)" format.
        if itype == "command_execution":
            totals["tool_calls"] += 1
            print(f"{TAG} [tool_use] Bash({_short(item.get('command', ''), 120)}) exit={item.get('exit_code')}")
        elif itype == "file_change":
            totals["tool_calls"] += 1
            changes = ", ".join(f"{c.get('kind')}:{c.get('path')}" for c in item.get("changes") or [])
            print(f"{TAG} [tool_use] Edit({_short(changes, 120)}) status={item.get('status')}")
        elif itype == "mcp_tool_call":
            totals["tool_calls"] += 1
            print(f"{TAG} [tool_use] {item.get('tool', 'mcp')}({_short(item.get('arguments', ''), 120)})")
        elif itype == "agent_message":
            print(f"{TAG} [assistant] {_short(item.get('text', ''), 200)}...")
        elif itype == "error":
            print(f"{TAG} [error] {_short(item.get('message', ''), 300)}")
    elif etype == "turn.completed":
        usage = event.get("usage") or {}
        for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                    "output_tokens", "reasoning_output_tokens"):
            totals[key] += usage.get(key) or 0
    elif etype == "turn.failed":
        totals["failed"] = True
        print(f"{TAG} [turn_failed] {_short((event.get('error') or {}).get('message', ''), 500)}", file=sys.stderr)
    elif etype == "error":
        print(f"{TAG} [stream_error] {_short(event.get('message', ''), 500)}", file=sys.stderr)


def _stream_events(pipe, totals: dict) -> None:
    """Save the raw JSONL events and log a readable summary of each one."""
    EVENTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_LOG_PATH.open("w", encoding="utf-8") as log_file:
        for line in iter(pipe.readline, ""):
            log_file.write(line)
            log_file.flush()
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                print(f"{TAG} [stdout] {_short(stripped, 300)}")
                continue
            if isinstance(event, dict):
                try:
                    _log_event(event, totals)
                except Exception as log_err:
                    print(f"{TAG} [log_error] {log_err}: {_short(stripped, 100)}")
        pipe.close()


def _stream_stderr(pipe) -> None:
    for line in iter(pipe.readline, ""):
        print(f"{TAG} [stderr] {line.rstrip()}", file=sys.stderr, flush=True)
    pipe.close()


PRICE_ENV = {"input": "CODEX_PRICE_INPUT", "cached": "CODEX_PRICE_CACHED", "output": "CODEX_PRICE_OUTPUT"}
# OpenRouter pricing fields are USD per token.
OPENROUTER_PRICE_FIELDS = {"input": "prompt", "cached": "input_cache_read", "output": "completion"}


def _openrouter_prices(model: str) -> dict | None:
    """USD per 1M tokens for `model` from OpenRouter's public model list."""
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as resp:
            models = json.load(resp).get("data") or []
    except Exception as e:
        print(f"{TAG} Could not fetch OpenRouter prices: {e}", file=sys.stderr)
        return None
    entry = next((m for m in models if m.get("id") == model), None)
    if not entry:
        print(f"{TAG} Model {model!r} not in OpenRouter's model list; no prices.", file=sys.stderr)
        return None
    pricing = entry.get("pricing") or {}
    # Ignores pricing["overrides"] (higher rates for requests above a prompt
    # size): Codex only reports per-turn totals, not per-request sizes.
    return {
        key: float(pricing.get(field) or 0) * 1_000_000
        for key, field in OPENROUTER_PRICE_FIELDS.items()
    }


def resolve_prices(provider_id: str, model: str) -> dict:
    """USD per 1M tokens: CODEX_PRICE_* if set, else OpenRouter's list, else 0."""
    if any(_env(name) for name in PRICE_ENV.values()):
        source = ".env (CODEX_PRICE_*)"
        prices = {}
        for key, name in PRICE_ENV.items():
            try:
                prices[key] = float(_env(name) or 0)
            except ValueError:
                prices[key] = 0.0
    elif provider_id == "openrouter" and (fetched := _openrouter_prices(model)):
        source, prices = "OpenRouter model list", fetched
    else:
        source, prices = "none (cost will be 0)", {key: 0.0 for key in PRICE_ENV}
    print(f"{TAG} Prices from {source}: input=${prices['input']:g}/M "
          f"cached=${prices['cached']:g}/M output=${prices['output']:g}/M")
    return prices


def print_result(totals: dict, prices: dict, duration: float) -> None:
    # OpenAI's input_tokens include the cached ones; report the uncached part
    # as "input" to match Claude Code's convention (input excludes cache_read).
    # output_tokens already include the reasoning tokens.
    cached = totals["cached_input_tokens"]
    uncached = max(totals["input_tokens"] - cached, 0)
    output = totals["output_tokens"]
    cost = (
        uncached * prices["input"]
        + cached * prices["cached"]
        + output * prices["output"]
    ) / 1_000_000
    # "turns" counts tool calls: `codex exec` runs the whole task as one turn.
    print(
        f"{TAG} [result] turns={totals['tool_calls']}  cost=${cost:.4f}  "
        f"input={uncached} output={output} cache_read={cached} "
        f"cache_write={totals['cache_write_input_tokens']} "
        f"reasoning={totals['reasoning_output_tokens']} "
        f"duration={duration:.1f}s"
    )


# ---------------------------------------------------------------------------
# Main agent run
# ---------------------------------------------------------------------------

def run_codex(prompt: str, model: str, prices: dict) -> int:
    command = [
        "codex", "exec",
        "--json",
        "--skip-git-repo-check",
        "--cd", str(CODE_ROOT),
        # The benchmark container is the sandbox; Codex's own Landlock/seccomp
        # sandbox doesn't reliably start inside Docker.
        "--dangerously-bypass-approvals-and-sandbox",
        prompt,
    ]
    print(f"{TAG} Starting Codex (model={model}, timeout={TIMEOUT_SECONDS}s, "
          f"reasoning_effort={REASONING_EFFORT or 'default'})")
    print(f"{TAG} Launching: {' '.join(command[:-1])} <PROMPT>")

    env = os.environ.copy()
    env["HOME"] = str(CODEX_HOME)
    env["CODEX_HOME"] = str(CODEX_HOME)

    start = time.monotonic()
    process = subprocess.Popen(
        command,
        text=True,
        cwd=str(CODE_ROOT),
        env=env,
        # Without this, codex exec waits for extra input on a piped stdin.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )

    totals = _new_usage_totals()
    stdout_thread = threading.Thread(target=_stream_events, args=(process.stdout, totals), daemon=True)
    stderr_thread = threading.Thread(target=_stream_stderr, args=(process.stderr,), daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        returncode = process.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        print_result(totals, prices, time.monotonic() - start)
        print(f"{TAG} Codex timed out after {TIMEOUT_SECONDS} seconds.", file=sys.stderr)
        return 124

    stdout_thread.join()
    stderr_thread.join()
    print_result(totals, prices, time.monotonic() - start)
    print(f"{TAG} Codex CLI finished with return code {returncode}")

    if returncode == 0 and totals["failed"]:
        return 1
    return returncode


def main() -> None:
    print(f"{TAG} Starting CVDP Codex agent...")

    codex_bin = shutil.which("codex")
    print(f"{TAG} codex CLI path: {codex_bin or 'NOT FOUND'}")
    if not codex_bin:
        print(f"{TAG} Agent failed.", file=sys.stderr)
        sys.exit(1)
    try:
        result = subprocess.run([codex_bin, "--version"], capture_output=True, text=True, timeout=10)
        print(f"{TAG} codex --version: {(result.stdout + result.stderr).strip()}")
    except Exception as e:
        print(f"{TAG} codex --version failed: {e}")

    provider_id, table, model = resolve_provider()
    print(f"{TAG} Provider: {provider_id} (LLM_MODEL={os.environ.get('LLM_MODEL', '')})")
    write_codex_config(provider_id, table, model)
    prices = resolve_prices(provider_id, model)

    payload = read_prompt_payload()
    prompt_text = payload.get("prompt", "")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        print(f"{TAG} No prompt found in /code/prompt.json. Exiting.", file=sys.stderr)
        sys.exit(1)

    prompt = build_cvdp_prompt(prompt_text, payload, code_root=CODE_ROOT)
    exit_code = run_codex(prompt, model, prices)

    if exit_code != 0:
        print(f"{TAG} Codex CLI failed.", file=sys.stderr)
        print(f"{TAG} Agent failed.", file=sys.stderr)
        sys.exit(exit_code)

    print(f"{TAG} Agent completed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
