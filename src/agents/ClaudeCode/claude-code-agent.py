#!/usr/bin/env python3

"""
CVDP agent that uses the Claude Code SDK to solve RTL benchmark tasks.

Expected flow:
1. CVDP mounts the datapoint workspace at /code and writes /code/prompt.json.
2. This script reads the task from /code/prompt.json.
3. Claude Code operates directly on /code using its native file tools.
4. CVDP runs its normal validation afterward.

Model / authentication — chosen by the provider prefix of LLM_MODEL:
- bedrock/<id>   -> AWS Bedrock (CLAUDE_CODE_USE_BEDROCK=1). AWS_ACCESS_KEY_ID /
  AWS_SECRET_ACCESS_KEY / AWS_REGION are forwarded by dataset_processor.py.
- vertex_ai/<id> -> Vertex AI: needs ANTHROPIC_VERTEX_PROJECT_ID + ANTHROPIC_VERTEX_LOCATION,
  authenticated through the gcloud ADC file mounted from the host.
- anthropic/<id> -> Direct API with ANTHROPIC_API_KEY.
- bare name      -> legacy: Vertex if ANTHROPIC_VERTEX_PROJECT_ID is set, else direct API.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from claude_code_sdk import ClaudeCodeOptions, query

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.cvdp_prompt import build_cvdp_prompt
from common.llm_model import split_llm_model

CODE_ROOT = Path("/code")
PROMPT_JSON_PATH = CODE_ROOT / "prompt.json"
LLM_PROVIDER, MODEL = split_llm_model()
MAX_TURNS = int(os.environ.get("CLAUDE_CODE_MAX_TURNS", "30"))


VERTEX_PROJECT = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip()
# `or`, not a get() default: the host forwards the var as "" when it's unset.
VERTEX_LOCATION = os.environ.get("ANTHROPIC_VERTEX_LOCATION", "").strip() or "us-east5"


# ---------------------------------------------------------------------------
# Authentication helpers
# ---------------------------------------------------------------------------

def _setup_writable_home() -> None:
    # Set up a writable HOME so the claude CLI can create ~/.claude/ config.
    # Without this, the CLI exits silently with rc=0 (same issue OpenCode had).
    claude_home = Path("/tmp/claude-code-home")
    claude_config = claude_home / ".claude"
    claude_config.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(claude_home)
    os.environ["XDG_CONFIG_HOME"] = str(claude_home / ".config")
    os.environ["XDG_CACHE_HOME"] = str(claude_home / ".cache")
    os.environ["XDG_DATA_HOME"] = str(claude_home / ".local" / "share")


def setup_auth() -> str:
    """
    Configure the auth environment for the claude CLI subprocess, based on
    the LLM_MODEL provider prefix. Returns 'bedrock', 'vertex' or 'direct'.

    Vertex strategy: use the gcloud ADC file mounted from the host
    ($CLOUDSDK_CONFIG/application_default_credentials.json). It contains a valid
    refresh_token so google-auth can renew the access token via HTTP without needing
    gcloud inside the container. This is more reliable than building a fake ADC file
    from a raw access_token (google-auth ignores refresh_token="" and fails silently).
    """
    if LLM_PROVIDER == "bedrock":
        _setup_writable_home()
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        if not os.environ.get("AWS_REGION"):
            os.environ["AWS_REGION"] = "us-east-1"
        # Background tasks default to a Haiku model the AWS account may not
        # have enabled: reuse the main model so every call hits the same one.
        os.environ.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", MODEL)
        if not os.environ.get("AWS_ACCESS_KEY_ID"):
            print(
                "[claude-code-agent.py] WARNING: Bedrock mode but AWS_ACCESS_KEY_ID is not set.",
                file=sys.stderr,
            )
        print(f"[claude-code-agent.py] Bedrock auth: region={os.environ['AWS_REGION']}")
        return "bedrock"

    if LLM_PROVIDER == "anthropic" or not VERTEX_PROJECT:
        if LLM_PROVIDER == "vertex_ai":
            print(
                "[claude-code-agent.py] WARNING: vertex_ai/ model but "
                "ANTHROPIC_VERTEX_PROJECT_ID is not set; falling back to direct API.",
                file=sys.stderr,
            )
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print(
                "[claude-code-agent.py] WARNING: Neither ANTHROPIC_VERTEX_PROJECT_ID "
                "nor ANTHROPIC_API_KEY is set.",
                file=sys.stderr,
            )
        return "direct"

    _setup_writable_home()

    # --- Vertex mode ---
    os.environ["ANTHROPIC_VERTEX_PROJECT_ID"] = VERTEX_PROJECT
    os.environ["ANTHROPIC_VERTEX_LOCATION"] = VERTEX_LOCATION
    # Tells the claude CLI to route through Vertex AI instead of api.anthropic.com
    os.environ["CLAUDE_CODE_USE_VERTEX"] = "1"
    os.environ["GOOGLE_CLOUD_PROJECT"] = VERTEX_PROJECT
    os.environ["VERTEX_LOCATION"] = VERTEX_LOCATION
    os.environ["GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES"] = "1"

    # Point GOOGLE_APPLICATION_CREDENTIALS at the gcloud user ADC file
    # mounted from the host — it has a real refresh_token that google-auth
    # can use over plain HTTPS without any gcloud binary inside the container.
    cloudsdk_config = os.environ.get("CLOUDSDK_CONFIG", "/gcp/gcloud-config")
    gcloud_adc = os.path.join(cloudsdk_config, "application_default_credentials.json")

    if os.path.isfile(gcloud_adc):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = gcloud_adc
        print(f"[claude-code-agent.py] Vertex auth: gcloud ADC → {gcloud_adc}")
    elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        print(
            f"[claude-code-agent.py] Vertex auth: GOOGLE_APPLICATION_CREDENTIALS="
            f"{os.environ['GOOGLE_APPLICATION_CREDENTIALS']}"
        )
    else:
        print(
            "[claude-code-agent.py] WARNING: Vertex mode but no Google credentials found.",
            file=sys.stderr,
        )

    return "vertex"


# ---------------------------------------------------------------------------
# Prompt / payload helpers
# ---------------------------------------------------------------------------

def read_prompt_payload() -> dict:
    try:
        with open(PROMPT_JSON_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
    except Exception as e:
        print(f"[claude-code-agent.py] Error reading prompt.json: {e}", file=sys.stderr)
        return {}


def build_prompt(prompt_text: str, payload: dict) -> str:
    return build_cvdp_prompt(prompt_text, payload, code_root=CODE_ROOT)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_message(msg) -> None:
    msg_type = type(msg).__name__
    try:
        if msg_type == "SystemMessage":
            print(f"[claude-code-agent.py] [system] {getattr(msg, 'subtype', '')}")
        elif msg_type == "AssistantMessage":
            for block in getattr(msg, "content", []):
                block_type = type(block).__name__
                if block_type == "TextBlock":
                    text = getattr(block, "text", "")
                    if text.strip():
                        print(f"[claude-code-agent.py] [assistant] {text[:200].replace(chr(10), ' ')}...")
                elif block_type == "ToolUseBlock":
                    print(f"[claude-code-agent.py] [tool_use] {getattr(block, 'name', '?')}({str(getattr(block, 'input', ''))[:120]})")
        elif msg_type == "ResultMessage":
            # SDK field is `total_cost_usd`; `cost_usd` kept as a fallback in case
            # of older/renamed SDK versions.
            cost = getattr(msg, "total_cost_usd", None)
            if cost is None:
                cost = getattr(msg, "cost_usd", None)
            cost_str = f"  cost=${cost:.4f}" if cost is not None else ""
            usage = getattr(msg, "usage", None)
            if usage is not None:
                inp = getattr(usage, "input_tokens", usage.get("input_tokens", "?") if isinstance(usage, dict) else "?")
                out = getattr(usage, "output_tokens", usage.get("output_tokens", "?") if isinstance(usage, dict) else "?")
                cache_read = getattr(usage, "cache_read_input_tokens", usage.get("cache_read_input_tokens", 0) if isinstance(usage, dict) else 0) or 0
                cache_write = getattr(usage, "cache_creation_input_tokens", usage.get("cache_creation_input_tokens", 0) if isinstance(usage, dict) else 0) or 0
                usage_str = f"  input={inp} output={out} cache_read={cache_read} cache_write={cache_write}"
            else:
                usage_str = ""
            print(f"[claude-code-agent.py] [result] turns={getattr(msg, 'num_turns', '?')}{cost_str}{usage_str}")
        else:
            print(f"[claude-code-agent.py] [msg:{msg_type}] {str(msg)[:200]}")
    except Exception as log_err:
        print(f"[claude-code-agent.py] [log_error] {log_err}: {str(msg)[:100]}")


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

def _diag_run(label: str, cmd: list, env: dict, timeout: int = 30) -> str:
    lines = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, cwd=str(CODE_ROOT),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        lines.append(f"[claude-code-agent.py] [diag:{label}] rc={proc.returncode}")
        if stdout.strip():
            lines.append(f"[claude-code-agent.py] [diag:{label}] stdout={stdout[:400]!r}")
        if stderr.strip():
            lines.append(f"[claude-code-agent.py] [diag:{label}] stderr={stderr[:400]!r}")
        if not stdout.strip() and not stderr.strip():
            lines.append(f"[claude-code-agent.py] [diag:{label}] (empty output)")
    except subprocess.TimeoutExpired:
        lines.append(f"[claude-code-agent.py] [diag:{label}] timeout after {timeout}s")
    except Exception as e:
        lines.append(f"[claude-code-agent.py] [diag:{label}] error: {e}")
    return "\n".join(lines)


def _diag_claude_direct() -> str:
    lines = []
    base_env = os.environ.copy()

    # Check if the gcloud user ADC file (with real refresh_token) is accessible
    cloudsdk_config = base_env.get("CLOUDSDK_CONFIG", "/gcp/gcloud-config")
    gcloud_adc = os.path.join(cloudsdk_config, "application_default_credentials.json")
    lines.append(f"[claude-code-agent.py] [diag:adc] {gcloud_adc} exists={os.path.isfile(gcloud_adc)}")
    if os.path.isfile(gcloud_adc):
        try:
            with open(gcloud_adc) as f:
                adc_type = json.load(f).get("type", "unknown")
            lines.append(f"[claude-code-agent.py] [diag:adc] type={adc_type}")
        except Exception as e:
            lines.append(f"[claude-code-agent.py] [diag:adc] read error: {e}")

    # Fake API key — longer timeout so we can see the auth error message
    fake_env = base_env.copy()
    fake_env.pop("CLAUDE_CODE_USE_VERTEX", None)
    fake_env.pop("CLAUDE_CODE_USE_BEDROCK", None)
    fake_env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    fake_env["ANTHROPIC_API_KEY"] = "sk-ant-fake-key-for-diag"
    lines.append(_diag_run("fake-key", ["claude", "-p", "say hi", "--dangerously-skip-permissions"], fake_env, timeout=90))

    # Vertex without GOOGLE_APPLICATION_CREDENTIALS — see if error changes
    if base_env.get("CLAUDE_CODE_USE_VERTEX"):
        no_creds_env = base_env.copy()
        no_creds_env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        lines.append(_diag_run("vertex-no-creds", ["claude", "-p", "say hi", "--dangerously-skip-permissions"], no_creds_env, timeout=30))

    return "\n".join(lines)


async def run_agent(prompt_text: str, payload: dict) -> int:
    diag_output = _diag_claude_direct()

    prompt = build_prompt(prompt_text, payload)

    opts = ClaudeCodeOptions(
        model=MODEL,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Write", "Edit", "MultiEdit", "Bash"],
        cwd=str(CODE_ROOT),
        max_turns=MAX_TURNS,
    )

    print(f"[claude-code-agent.py] Starting Claude Code (model={MODEL}, max_turns={MAX_TURNS}, max_output_tokens={os.environ.get('CLAUDE_CODE_MAX_OUTPUT_TOKENS', 'default')})")

    try:
        async for message in query(prompt=prompt, options=opts):
            _log_message(message)
        return 0
    except Exception as e:
        print(diag_output, file=sys.stderr)
        print(f"[claude-code-agent.py] Claude Code error: {e}", file=sys.stderr)
        return 1


def main() -> None:
    print("[claude-code-agent.py] Starting CVDP Claude Code agent...")

    claude_bin = shutil.which("claude")
    print(f"[claude-code-agent.py] claude CLI path: {claude_bin or 'NOT FOUND'}")
    if claude_bin:
        try:
            result = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=10)
            print(f"[claude-code-agent.py] claude --version: {(result.stdout + result.stderr).strip()}")
        except Exception as e:
            print(f"[claude-code-agent.py] claude --version failed: {e}")

    # Also show where npm global binaries are installed
    try:
        npm_bin = subprocess.run(["npm", "bin", "-g"], capture_output=True, text=True, timeout=10)
        npm_bin_dir = npm_bin.stdout.strip()
        print(f"[claude-code-agent.py] npm global bin: {npm_bin_dir}")
        npm_claude = os.path.join(npm_bin_dir, "claude")
        if os.path.exists(npm_claude):
            print(f"[claude-code-agent.py] npm claude found: {npm_claude}")
        else:
            print(f"[claude-code-agent.py] npm claude NOT found at: {npm_claude}")
    except Exception as e:
        print(f"[claude-code-agent.py] npm bin check failed: {e}")

    auth_mode = setup_auth()
    print(f"[claude-code-agent.py] Auth mode: {auth_mode}")

    payload = read_prompt_payload()
    prompt_text = payload.get("prompt", "")
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        print("[claude-code-agent.py] No prompt found in /code/prompt.json. Exiting.", file=sys.stderr)
        sys.exit(1)

    exit_code = asyncio.run(run_agent(prompt_text, payload))

    if exit_code != 0:
        print("[claude-code-agent.py] Agent failed.", file=sys.stderr)
        sys.exit(exit_code)

    print("[claude-code-agent.py] Agent completed successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
