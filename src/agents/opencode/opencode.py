#!/usr/bin/env python3

"""
CVDP agent entrypoint that acts as a thin wrapper around an OpenCode runner.

Expected flow:
1. CVDP mounts the datapoint workspace at /code and writes /code/prompt.json.
2. This script reads the benchmark task from /code/prompt.json.
3. This script invokes an external OpenCode bridge script.
4. OpenCode operates directly on /code.
5. CVDP runs its normal validation afterward.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.cvdp_prompt import build_cvdp_prompt
from common.llm_model import split_llm_model

CODE_ROOT = Path("/code")
PROMPT_JSON_PATH = CODE_ROOT / "prompt.json"
DEFAULT_AGENT = os.environ.get("OPENCODE_AGENT", "build")
DEFAULT_OPENCODE_EXECUTABLE = os.environ.get("OPENCODE_EXECUTABLE", "opencode")
# Files di log
STDOUT_LOG_PATH = CODE_ROOT / "opencode_stdout.log"
STDERR_LOG_PATH = CODE_ROOT / "opencode_stderr.log"

HOME_DIR = Path("/tmp/opencode-home")
XDG_CONFIG_HOME = HOME_DIR / ".config"
XDG_CACHE_HOME = HOME_DIR / ".cache"
XDG_DATA_HOME = HOME_DIR / ".local" / "share"

DEFAULT_TIMEOUT = 600
# --iter-to-convergence: the orchestrator mounts the loop state here, and the
# OpenCode home above from the host, so the session id survives between runs
ITC_CTL_DIR = Path("/tmp/cvdp_itc")
ITC_SESSION_FILE = HOME_DIR / "cvdp_session_id"


def read_itc_state() -> dict | None:
    """Loop state written by the orchestrator, or None outside --iter-to-convergence."""
    try:
        state = json.loads((ITC_CTL_DIR / "state.json").read_text(encoding="utf-8"))
        state["feedback"] = (ITC_CTL_DIR / "feedback.md").read_text(encoding="utf-8")
        return state
    except (OSError, ValueError):
        return None

def read_prompt_payload() -> dict:
    """Read the full CVDP prompt payload from /code/prompt.json."""
    try:
        with open(PROMPT_JSON_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
    except Exception as e:
        print(f"Error reading prompt.json: {e}")
        return {}


def read_prompt() -> str:
    """Read only the prompt text from the CVDP payload."""
    payload = read_prompt_payload()
    prompt = payload.get("prompt", "")
    return prompt if isinstance(prompt, str) else ""

def build_opencode_prompt(prompt_text: str, payload: dict | None = None, itc: dict | None = None) -> str:
    """Build a benchmark-aware prompt to pass directly to the OpenCode CLI."""
    if not itc:
        return build_cvdp_prompt(prompt_text, payload, code_root=CODE_ROOT)
    return build_cvdp_prompt(
        prompt_text, payload, code_root=CODE_ROOT, iterate_to_convergence=True,
        category=itc.get("category"), harness_visible=bool(itc.get("harness_visible")),
        harness_root=itc.get("harness_root"), heavy=bool(itc.get("heavy")))

def _stream_pipe(pipe, log_path: Path, mirror_to_stderr: bool = False) -> None:
    """Stream a subprocess pipe live to both console and a log file."""
    with log_path.open("w", encoding="utf-8") as log_file:
        for line in iter(pipe.readline, ""):
            log_file.write(line)
            log_file.flush()
            if mirror_to_stderr:
                print(line, end="", file=sys.stderr, flush=True)
            else:
                print(line, end="", flush=True)
        pipe.close()


def _new_usage_totals() -> dict:
    return {
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "tool_counts": {},
        "session_id": None,
    }


def _stream_stdout_and_collect_usage(pipe, log_path: Path, totals: dict) -> None:
    """Stream OpenCode's --format json event lines to console/log, accumulating usage as we go.

    Each `step_finish` event carries the cost/tokens for just that step (not
    cumulative), so summing across every step gives the true run total.
    """
    with log_path.open("w", encoding="utf-8") as log_file:
        for line in iter(pipe.readline, ""):
            log_file.write(line)
            log_file.flush()
            print(line, end="", flush=True)

            stripped = line.strip()
            if stripped:
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    event = None
                if isinstance(event, dict):
                    if not totals["session_id"] and event.get("sessionID"):
                        totals["session_id"] = event["sessionID"]
                    etype = event.get("type")
                    part = event.get("part") if isinstance(event.get("part"), dict) else {}
                    if etype == "step_finish":
                        totals["cost_usd"] += part.get("cost") or 0
                        tokens = part.get("tokens") or {}
                        totals["prompt_tokens"] += tokens.get("input") or 0
                        totals["completion_tokens"] += tokens.get("output") or 0
                        totals["reasoning_tokens"] += tokens.get("reasoning") or 0
                        cache = tokens.get("cache") or {}
                        totals["cache_read_tokens"] += cache.get("read") or 0
                        totals["cache_write_tokens"] += cache.get("write") or 0
                    elif etype == "tool_use":
                        tool_name = part.get("tool")
                        if tool_name:
                            totals["tool_counts"][tool_name] = totals["tool_counts"].get(tool_name, 0) + 1
        pipe.close()


def _print_usage_summary(totals: dict) -> None:
    total_tokens = totals["prompt_tokens"] + totals["completion_tokens"]
    print(
        f"[agent.py] usage_summary cost_usd={totals['cost_usd']:.6f} "
        f"prompt_tokens={totals['prompt_tokens']} "
        f"completion_tokens={totals['completion_tokens']} "
        f"cache_read_tokens={totals['cache_read_tokens']} "
        f"cache_write_tokens={totals['cache_write_tokens']} "
        f"reasoning_tokens={totals['reasoning_tokens']} "
        f"total_tokens={total_tokens}",
        flush=True,
    )
    if totals["tool_counts"]:
        tool_summary = " ".join(f"{name}={count}" for name, count in sorted(totals["tool_counts"].items()))
        print(f"[agent.py] tool_usage {tool_summary}", flush=True)

def _save_itc_session(itc: dict | None, totals: dict) -> None:
    """Remember the session id so the next iteration can resume it."""
    if itc and totals["session_id"]:
        ITC_SESSION_FILE.write_text(totals["session_id"], encoding="utf-8")
        print(f"[agent.py] Session id: {totals['session_id']}", flush=True)

def ensure_runtime_dirs() -> None:
    """Create writable runtime directories for Bun/OpenCode."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    XDG_CONFIG_HOME.mkdir(parents=True, exist_ok=True)
    XDG_CACHE_HOME.mkdir(parents=True, exist_ok=True)
    XDG_DATA_HOME.mkdir(parents=True, exist_ok=True)

def write_opencode_config(provider_id: str | None = None, provider: dict | None = None) -> None:
    """Write the OpenCode config, optionally registering `provider_id` (a custom
    provider, or extra models/options for a built-in one)."""
    config_dir = XDG_CONFIG_HOME / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "$schema": "https://opencode.ai/config.json",
        # `opencode run` is headless: any permission that resolves to "ask" is
        # auto-rejected and ends the session. Newer OpenCode asks for paths
        # outside --dir (e.g. `cd /tmp && iverilog ...`), so allow everything;
        # the container is the sandbox.
        "permission": {"*": "allow", "external_directory": "allow"},
    }
    if provider_id:
        config["provider"] = {provider_id: provider}
    config_path = config_dir / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print(f"[agent.py] Wrote OpenCode config to {config_path}")

def resolve_opencode_model(provider: str, model_name: str) -> str:
    """Translate LLM_MODEL (split by split_llm_model) to the provider/model
    format expected by OpenCode."""
    model_name = model_name.strip()
    if not model_name:
        return ""
    if provider == "bedrock":
        return f"amazon-bedrock/{model_name}"
    if provider == "vertex_ai":
        if model_name.lower().startswith("claude"):
            return f"google-vertex-anthropic/{model_name}"
        return f"google-vertex/{model_name}"
    if provider:
        # anthropic/, openai/, openrouter/ use the same provider ids in OpenCode.
        return f"{provider}/{model_name}"
    if "/" in model_name:
        return model_name

    lower_name = model_name.lower()

    # Gemini shorthand -> OpenRouter Google model
    if "gemini" in lower_name:
        return f"openrouter/google/{model_name}"

    # GPT shorthand -> OpenAI model
    if lower_name.startswith("gpt"):
        return f"openai/{model_name}"


    # Claude 3.7 shorthand -> custom OpenRouter provider (registered via config file)
    if lower_name.startswith("claude-3.7") or lower_name.startswith("claude-3-7"):
        openrouter_id = model_name.replace(".", "-")
        return f"openrouter/anthropic/{openrouter_id}"

    # Claude shorthand -> Anthropic model - VERTEX
    if lower_name.startswith("claude"):
        return f"google-vertex-anthropic/{model_name}"

    # Fallback: let OpenCode receive the original string if no rule matches.
    return model_name

def run_opencode(prompt_text: str, payload: dict | None = None) -> int:
    """Invoke the OpenCode CLI directly in non-interactive mode."""
    itc = read_itc_state()
    prompt = build_opencode_prompt(prompt_text, payload, itc)
    timeout = DEFAULT_TIMEOUT
    resume_session = None
    if itc:
        timeout = itc.get("agent_timeout") or None
        print(f"[agent.py] iterate-to-convergence: iteration {itc.get('iteration')}/{itc.get('max_iterations')}")
        if itc.get("feedback"):
            # Resume the previous session with the harness output; if its id was
            # lost, start over with the full prompt followed by the feedback.
            try:
                resume_session = ITC_SESSION_FILE.read_text(encoding="utf-8").strip() or None
            except OSError:
                resume_session = None
            prompt = itc["feedback"] if resume_session else f"{prompt}\n\n{itc['feedback']}"
            print(f"[agent.py] Resuming session: {resume_session or 'none found, new session'}")

    command = [
        DEFAULT_OPENCODE_EXECUTABLE,
        "run",
        "--dir",
        str(CODE_ROOT),
        "--agent",
        DEFAULT_AGENT,
        "--print-logs",
        "--log-level",
        "DEBUG",
        "--format",
        "json",
    ]

    resolved_model = resolve_opencode_model(*split_llm_model())
    if resolved_model:
        print(f"[agent.py] Using explicit model: {resolved_model}")
        command.extend([
            "--model",
            resolved_model,
        ])
    else:
        print("[agent.py] No explicit OpenCode model passed; using CLI default")

    if resume_session:
        command.extend(["--session", resume_session])

    command.append(prompt)

    print(f"[agent.py] Launching OpenCode CLI: {' '.join(command[:-1])} <PROMPT>")

    ensure_runtime_dirs()

    env = os.environ.copy()

    # Fix env for Vertex Claude (OpenCode expects different names from what the AnthropicVertex client library uses, so we need to set both)
    if "ANTHROPIC_VERTEX_PROJECT_ID" in env:
        env["GOOGLE_CLOUD_PROJECT"] = env["ANTHROPIC_VERTEX_PROJECT_ID"]

    if "ANTHROPIC_VERTEX_LOCATION" in env:
        env["VERTEX_LOCATION"] = env["ANTHROPIC_VERTEX_LOCATION"]


    # Always write the config (permissions); provider branches below overwrite it.
    write_opencode_config()

    # OpenRouter: write config file so OpenCode registers the provider and model
    openrouter_key = env.get("OPENROUTER_API_KEY", "")
    if openrouter_key and "openrouter" in resolved_model:
        # model_id is everything after "openrouter/"
        openrouter_model_id = resolved_model.split("openrouter/", 1)[1]
        write_opencode_config("openrouter", {
            "name": "OpenRouter",
            "options": {
                "apiKey": openrouter_key,
                "baseURL": "https://openrouter.ai/api/v1",
            },
            "models": {openrouter_model_id: {"name": openrouter_model_id}},
        })

    # Bedrock: OpenCode's amazon-bedrock provider reads AWS_ACCESS_KEY_ID /
    # AWS_SECRET_ACCESS_KEY from env. Register the model explicitly so ids not
    # (yet) in OpenCode's model catalog still resolve, and pin the region.
    if resolved_model.startswith("amazon-bedrock/"):
        bedrock_model_id = resolved_model.split("amazon-bedrock/", 1)[1]
        write_opencode_config("amazon-bedrock", {
            "options": {"region": env.get("AWS_REGION") or "us-east-1"},
            "models": {bedrock_model_id: {"name": bedrock_model_id}},
        })

    env["HOME"] = str(HOME_DIR)
    env["XDG_CONFIG_HOME"] = str(XDG_CONFIG_HOME)
    env["XDG_CACHE_HOME"] = str(XDG_CACHE_HOME)
    env["XDG_DATA_HOME"] = str(XDG_DATA_HOME)

    STDOUT_LOG_PATH.write_text("", encoding="utf-8")
    STDERR_LOG_PATH.write_text("", encoding="utf-8")

    print(f"[agent.py] STDOUT log path: {STDOUT_LOG_PATH}")
    print(f"[agent.py] STDERR log path: {STDERR_LOG_PATH}")
    print(f"[agent.py] /code contents before launch: {list(CODE_ROOT.iterdir())}")

    print(f"[agent.py] Effective LLM_MODEL env: {env.get('LLM_MODEL')}")
    print(f"[agent.py] Full command: {command}")

    process = subprocess.Popen(
        command,
        text=True,
        cwd="/app",
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )

    usage_totals = _new_usage_totals()

    stdout_thread = threading.Thread(
        target=_stream_stdout_and_collect_usage,
        args=(process.stdout, STDOUT_LOG_PATH, usage_totals),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_stream_pipe,
        args=(process.stderr, STDERR_LOG_PATH, True),
        daemon=True,
    )

    stdout_thread.start()
    stderr_thread.start()

    # Wait for process to complete with a timeout, then join threads and read logs
    try:
        returncode = process.wait(timeout=timeout)  # Changed timeout from 120 bc too short for a lot of datapoints - maybe 600 would be better?
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        print(f"[agent.py] OpenCode timed out after {timeout} seconds.", file=sys.stderr)
        # Steps completed before the kill were still billed — report them.
        _print_usage_summary(usage_totals)
        _save_itc_session(itc, usage_totals)
        return 124

    stdout_thread.join()
    stderr_thread.join()

    _print_usage_summary(usage_totals)
    _save_itc_session(itc, usage_totals)

    print(f"[agent.py] OpenCode CLI finished with return code {returncode}")
    return returncode

def main() -> None:
    print("[agent.py] Starting CVDP OpenCode wrapper...")

    payload = read_prompt_payload()
    prompt_text = payload.get("prompt", "") if isinstance(payload.get("prompt", ""), str) else ""
    if not prompt_text:
        print("[agent.py] No prompt found in /code/prompt.json. Exiting.", file=sys.stderr)
        sys.exit(1)

    exit_code = run_opencode(prompt_text, payload)
    if exit_code != 0:
        print("[agent.py] OpenCode CLI failed.", file=sys.stderr)
        sys.exit(exit_code)

    print("[agent.py] Wrapper completed successfully")
    sys.exit(0)

if __name__ == "__main__":
    main()