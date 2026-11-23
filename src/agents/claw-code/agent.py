#!/usr/bin/env python3

"""
CVDP agent entrypoint for Claw Code.

Expected flow:
1. CVDP mounts the datapoint workspace at /code and writes /code/prompt.json.
2. This script reads the benchmark task from /code/prompt.json.
3. This script invokes the external `claw` CLI in one-shot prompt mode.
4. Claw operates directly on /code.
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

DEFAULT_PERMISSION_MODE = os.environ.get("CLAW_PERMISSION_MODE", "danger-full-access")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("CLAW_TIMEOUT_SECONDS", "600"))
SKIP_PERMISSIONS = os.environ.get("CLAW_SKIP_PERMISSIONS", "").lower() in {"1", "true", "yes"}
VERTEX_MODEL_PREFIX = "google-vertex-anthropic/"

STDOUT_LOG_PATH = CODE_ROOT / "claw_stdout.log"
STDERR_LOG_PATH = CODE_ROOT / "claw_stderr.log"

HOME_DIR = Path("/tmp/claw-home")
XDG_CONFIG_HOME = HOME_DIR / ".config"
XDG_CACHE_HOME = HOME_DIR / ".cache"
XDG_DATA_HOME = HOME_DIR / ".local" / "share"


def read_prompt_payload() -> dict:
    """Read the full CVDP prompt payload from /code/prompt.json."""
    try:
        with PROMPT_JSON_PATH.open("r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        print(f"[claw-agent.py] Error reading prompt.json: {exc}", file=sys.stderr)
        return {}


def build_claw_prompt(prompt_text: str, payload: dict | None = None) -> str:
    """Build a benchmark-aware prompt to pass directly to the Claw CLI."""
    return build_cvdp_prompt(prompt_text, payload, code_root=CODE_ROOT)


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


def ensure_runtime_dirs() -> None:
    """Create writable runtime directories for Claw Code."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    XDG_CONFIG_HOME.mkdir(parents=True, exist_ok=True)
    XDG_CACHE_HOME.mkdir(parents=True, exist_ok=True)
    XDG_DATA_HOME.mkdir(parents=True, exist_ok=True)


def resolve_claw_model(provider: str, model_name: str) -> str:
    """Translate LLM_MODEL (split by split_llm_model) to the model name passed to Claw Code."""
    model_name = model_name.strip()
    if not model_name:
        return ""
    if provider == "vertex_ai":
        return f"{VERTEX_MODEL_PREFIX}{model_name}"
    if provider == "bedrock":
        print("[claw-agent.py] WARNING: Claw Code has no Bedrock support wired in; "
              "passing the model id through as is.", file=sys.stderr)
    if provider in {"bedrock", "anthropic"}:
        return model_name
    if provider:
        return f"{provider}/{model_name}"
    if model_name.startswith(VERTEX_MODEL_PREFIX) or model_name.startswith("vertex-anthropic/"):
        return model_name
    if os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") and model_name.lower().startswith("claude"):
        return f"{VERTEX_MODEL_PREFIX}{model_name}"
    if os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID") and model_name.lower() in {"opus", "sonnet", "haiku"}:
        return f"{VERTEX_MODEL_PREFIX}{model_name}"
    return model_name


def build_command(prompt: str) -> list[str]:
    """Build the non-interactive Claw command."""
    command = ["claw"]

    resolved_model = resolve_claw_model(*split_llm_model())
    if resolved_model:
        command.extend(["--model", resolved_model])

    if SKIP_PERMISSIONS:
        command.append("--dangerously-skip-permissions")
    elif DEFAULT_PERMISSION_MODE.strip():
        command.extend(["--permission-mode", DEFAULT_PERMISSION_MODE.strip()])

    command.extend(["--output-format", "text", "prompt", prompt])
    return command


def run_claw(prompt_text: str, payload: dict | None = None) -> int:
    """Invoke Claw Code in non-interactive mode."""
    prompt = build_claw_prompt(prompt_text, payload)
    command = build_command(prompt)

    ensure_runtime_dirs()

    env = os.environ.copy()
    env["HOME"] = str(HOME_DIR)
    env["XDG_CONFIG_HOME"] = str(XDG_CONFIG_HOME)
    env["XDG_CACHE_HOME"] = str(XDG_CACHE_HOME)
    env["XDG_DATA_HOME"] = str(XDG_DATA_HOME)
    if "ANTHROPIC_VERTEX_PROJECT_ID" in env:
        env["GOOGLE_CLOUD_PROJECT"] = env["ANTHROPIC_VERTEX_PROJECT_ID"]
    if "ANTHROPIC_VERTEX_LOCATION" in env:
        env["VERTEX_LOCATION"] = env["ANTHROPIC_VERTEX_LOCATION"]

    STDOUT_LOG_PATH.write_text("", encoding="utf-8")
    STDERR_LOG_PATH.write_text("", encoding="utf-8")

    print(f"[claw-agent.py] STDOUT log path: {STDOUT_LOG_PATH}")
    print(f"[claw-agent.py] STDERR log path: {STDERR_LOG_PATH}")
    print(f"[claw-agent.py] /code contents before launch: {list(CODE_ROOT.iterdir())}")
    print(f"[claw-agent.py] Effective LLM_MODEL env: {env.get('LLM_MODEL')}")
    print(f"[claw-agent.py] Resolved Claw model: {command[command.index('--model') + 1] if '--model' in command else ''}")
    print(f"[claw-agent.py] Effective permission mode: {DEFAULT_PERMISSION_MODE}")
    print(f"[claw-agent.py] Launching Claw CLI: {' '.join(command[:-1])} <PROMPT>")

    process = subprocess.Popen(
        command,
        text=True,
        cwd=str(CODE_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )

    stdout_thread = threading.Thread(
        target=_stream_pipe,
        args=(process.stdout, STDOUT_LOG_PATH, False),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_stream_pipe,
        args=(process.stderr, STDERR_LOG_PATH, True),
        daemon=True,
    )

    stdout_thread.start()
    stderr_thread.start()

    try:
        returncode = process.wait(timeout=DEFAULT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        print(f"[claw-agent.py] Claw timed out after {DEFAULT_TIMEOUT_SECONDS} seconds.", file=sys.stderr)
        return 124

    stdout_thread.join()
    stderr_thread.join()

    print(f"[claw-agent.py] Claw CLI finished with return code {returncode}")
    return returncode


def main() -> None:
    print("[claw-agent.py] Starting CVDP Claw Code wrapper...")

    payload = read_prompt_payload()
    prompt_text = payload.get("prompt", "") if isinstance(payload.get("prompt", ""), str) else ""
    if not prompt_text:
        print("[claw-agent.py] No prompt found in /code/prompt.json. Exiting.", file=sys.stderr)
        sys.exit(1)

    exit_code = run_claw(prompt_text, payload)
    if exit_code != 0:
        print("[claw-agent.py] Claw CLI failed.", file=sys.stderr)
        sys.exit(exit_code)

    print("[claw-agent.py] Wrapper completed successfully")
    sys.exit(0)


if __name__ == "__main__":
    main()
