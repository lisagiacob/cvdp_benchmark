"""
log_mining_agentic.py

Per ogni run analizza due livelli:

  1. Agentic loop  — cosa è successo dentro il processo agente
     Supporta tre formati:
       • claude_code  (claude-code-agent.py)
       • codex        (codex-agent.py — stesso formato di claude_code)
       • opencode     (agent.py + opencode CLI)

  2. Datapoint     — se il loop ha completato MA il datapoint è fallito,
     spiega perché leggendo harness/<run_id>/rtl/ e rundir/

Ground truth per pass/fail: --report <report.txt> (harness output).
Se non fornito, il datapoint_outcome rimane "unknown".

Output in --out (default: Logs/):
  agent_runs.csv            — una riga per run, entrambi i livelli
                               (colonne costo $ e token: opencode dalla riga "usage_summary" stampata da agent.py,
                                claude_code dalla riga "[result] ... cost=... input=...")
  agent_failure_reasons.csv — una riga per (run, reason), per pivot
  agent_summary.txt         — riepilogo testuale aggregato (incluso costo/token totali)
"""

from collections import defaultdict
import glob as glob_mod
import json
import re
import os
import csv
import argparse
import xml.etree.ElementTree as ET
from typing import Dict, List, Set, Tuple, Optional

parser = argparse.ArgumentParser()
parser.add_argument("work_dir", nargs="?", default="work")
parser.add_argument("--report", default=None,
                    help="Path to harness report.txt (default: <work_dir>/report.txt)")
parser.add_argument("--out", default="Logs")
args = parser.parse_args()

work_dir    = args.work_dir
report_path = args.report if args.report is not None else os.path.join(work_dir, "report.txt")
out_dir     = args.out
os.makedirs(out_dir, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Patterns — Claude Code loop
# ═══════════════════════════════════════════════════════════════════════════════

CC_FAILURE_PATTERNS: List[tuple] = [
    (re.compile(r"Claude's response exceeded the \d+ output token maximum", re.IGNORECASE),
     "MAX_OUTPUT_TOKENS"),
    (re.compile(r"Fatal error in message reader", re.IGNORECASE),
     "FATAL_MESSAGE_READER"),
    (re.compile(r"Command failed with exit code", re.IGNORECASE),
     "CLI_EXIT_CODE_ERROR"),
    (re.compile(r"\[diag:vertex-no-creds\] timeout", re.IGNORECASE),
     "VERTEX_AUTH_TIMEOUT"),
    (re.compile(r"There'?s an issue with the selected model", re.IGNORECASE),
     "SUBAGENT_MODEL_ERROR"),
    (re.compile(r"model_not_found|model.+not.+available", re.IGNORECASE),
     "MODEL_NOT_AVAILABLE"),
    (re.compile(r"context_length_exceeded|ContextWindowExceeded", re.IGNORECASE),
     "CONTEXT_LENGTH_EXCEEDED"),
    (re.compile(r"FreeUsageLimitError", re.IGNORECASE), "API_RATE_LIMIT"),
    (re.compile(r"Rate limit exceeded", re.IGNORECASE),  "API_RATE_LIMIT"),
    (re.compile(r"statusCode[\"']?\s*:\s*429", re.IGNORECASE), "API_RATE_LIMIT"),
    (re.compile(r"(?<!\d)429(?!\d)"),                    "API_RATE_LIMIT"),
    (re.compile(r"overloaded_error", re.IGNORECASE),     "API_OVERLOADED"),
    # (?!=) skips codex-agent.py's "Starting Codex (..., timeout=600s, ...)" line.
    (re.compile(r"timed out|timeout(?!=)", re.IGNORECASE), "TIMEOUT"),
    (re.compile(r"Permission denied", re.IGNORECASE),    "PERMISSION_DENIED"),
]

CC_TURNS_RE     = re.compile(r"\[result\] turns=(\d+)")
CC_MAX_TURNS_RE = re.compile(r"max_turns=(\d+)")
CC_TOOL_RE      = re.compile(r"\[tool_use\] (\w+)\(")
CC_MODEL_RE     = re.compile(r"model=([^\s,)]+)")
CC_SUCCESS_RE   = re.compile(r"Agent completed successfully\.")
CC_FAILED_RE    = re.compile(r"Agent failed\.")

# From _log_message()'s ResultMessage branch in claude-code-agent.py:
#   [result] turns=9  cost=$0.3781  input=158393 output=15109 cache_read=128260 cache_write=23413
CC_USAGE_RE = re.compile(
    r"\[result\].*?cost=\$([\d.]+)\s+input=(\d+)\s+output=(\d+)\s+cache_read=(\d+)\s+cache_write=(\d+)"
)
# Only codex-agent.py appends these to the [result] line:
#   ... cache_write=0 reasoning=5123 duration=312.4s
CC_REASONING_RE = re.compile(r"\[result\].*?\breasoning=(\d+)")
CC_DURATION_RE  = re.compile(r"\[result\].*?\bduration=([\d.]+)s")


# ═══════════════════════════════════════════════════════════════════════════════
# Patterns — OpenCode loop
# ═══════════════════════════════════════════════════════════════════════════════

OC_ERROR_LINE_RE = re.compile(r"^ERROR\s+\S+\s+.*?error=(\{.+\})\s*$")
OC_STEP_RE       = re.compile(r"service=session\.prompt step=(\d+)")
OC_MODEL_RE      = re.compile(r"modelID=([^\s]+)")
OC_TOOL_ARROW_RE = re.compile(r"^→ (\w+)")
OC_TOOL_BASH_RE  = re.compile(r"^\$\s+\S")
OC_SUCCESS_RE    = re.compile(r"\[agent\.py\].*(?:completed|success)", re.IGNORECASE)
OC_FAILED_RE     = re.compile(r"\[agent\.py\].*(?:failed|error)", re.IGNORECASE)
OC_DOOM_LOOP_RE  = re.compile(r"permission=doom_loop.*action.*ask|doom_loop.*triggered",
                               re.IGNORECASE)

# Emitted by agent.py itself (from --format json event accounting) — see
# _stream_stdout_and_collect_usage() in src/agents/opencode/agent.py.
OC_USAGE_RE = re.compile(
    r"\[agent\.py\] usage_summary cost_usd=([\d.]+) "
    r"prompt_tokens=(\d+) completion_tokens=(\d+) "
    r"cache_read_tokens=(\d+) cache_write_tokens=(\d+) "
    r"reasoning_tokens=(\d+) total_tokens=(\d+)"
)
OC_TOOL_USAGE_RE  = re.compile(r"\[agent\.py\] tool_usage (.+)")
OC_TOOL_COUNT_RE  = re.compile(r"(\w+)=(\d+)")


def _sum_oc_json_events(text: str) -> Optional[Dict]:
    """Fallback for --format json runs whose usage_summary line is missing
    (container killed externally, or older wrapper that skipped it on timeout):
    re-sum the per-step `step_finish` events and count `tool_use` events, same
    accounting as agent.py. Returns None if the log has no step_finish events."""
    totals = {"cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
              "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
              "tool_counts": defaultdict(int)}
    seen_step = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('{"type":'):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") == "step_finish":
            seen_step = True
            totals["cost_usd"] += part.get("cost") or 0
            tokens = part.get("tokens") or {}
            totals["prompt_tokens"]     += tokens.get("input") or 0
            totals["completion_tokens"] += tokens.get("output") or 0
            totals["reasoning_tokens"]  += tokens.get("reasoning") or 0
            cache = tokens.get("cache") or {}
            totals["cache_read_tokens"]  += cache.get("read") or 0
            totals["cache_write_tokens"] += cache.get("write") or 0
        elif event.get("type") == "tool_use" and part.get("tool"):
            totals["tool_counts"][part["tool"]] += 1
    return totals if seen_step else None


def _parse_oc_error_json(json_str: str) -> Dict:
    try:
        obj = json.loads(json_str)
        err = obj.get("error", obj)
        status = (err.get("statusCode") or err.get("status_code")
                  or err.get("code") or "")
        name  = err.get("name", "")
        inner = err.get("error", {})
        msg   = (inner.get("message", "") if isinstance(inner, dict) else "")
        return {"status_code": str(status), "name": name, "message": msg[:200]}
    except Exception:
        return {"status_code": "", "name": "", "message": json_str[:200]}


def _oc_status_to_label(info: Dict) -> Optional[str]:
    sc  = info["status_code"]
    name = info["name"].lower()
    msg  = info["message"].lower()
    if sc == "404" or "not_found" in name or "not found" in msg:
        return "MODEL_NOT_FOUND"
    if sc == "429" or "rate" in msg or "quota" in msg:
        return "API_RATE_LIMIT"
    if sc == "529" or "overloaded" in msg:
        return "API_OVERLOADED"
    if sc in ("500", "503"):
        return "API_SERVER_ERROR"
    if "context_length" in msg or "context window" in msg:
        return "CONTEXT_LENGTH_EXCEEDED"
    if "max_tokens" in msg or "output token" in msg:
        return "MAX_OUTPUT_TOKENS"
    if sc:
        return f"API_ERROR_{sc}"
    if name:
        return f"LLM_ERROR_{name.upper()[:30]}"
    return "UNKNOWN_LLM_ERROR"


# ═══════════════════════════════════════════════════════════════════════════════
# Agent type detection
# ═══════════════════════════════════════════════════════════════════════════════

def detect_agent_type(text: str) -> str:
    if "[claude-code-agent.py]" in text:
        return "claude_code"
    if "[codex-agent.py]" in text:
        return "codex"
    if "service=default version=" in text or "[agent.py]" in text:
        return "opencode"
    return "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Parser — Claude Code
# ═══════════════════════════════════════════════════════════════════════════════

def parse_claude_code(text: str) -> Dict:
    r: Dict = {
        "completed":    bool(CC_SUCCESS_RE.search(text)),
        "failed":       bool(CC_FAILED_RE.search(text)),
        "turns":        None,
        "max_turns":    None,
        "max_turns_reached": False,
        "model":        None,
        "loop_failure_reasons": [],
        "tool_counts":  defaultdict(int),
        "cost_usd":            None,
        "prompt_tokens":       None,
        "completion_tokens":   None,
        "cache_read_tokens":   None,
        "cache_write_tokens":  None,
        "reasoning_tokens":    None,
        "total_tokens":        None,
        "duration_s":          None,
    }

    m = CC_TURNS_RE.search(text)
    if m:
        r["turns"] = int(m.group(1))

    m = CC_MAX_TURNS_RE.search(text)
    if m:
        r["max_turns"] = int(m.group(1))

    if r["turns"] is not None and r["max_turns"] is not None:
        r["max_turns_reached"] = r["turns"] >= r["max_turns"]

    m = CC_MODEL_RE.search(text)
    if m:
        r["model"] = m.group(1)

    m = CC_USAGE_RE.search(text)
    if m:
        r["cost_usd"]           = float(m.group(1))
        r["prompt_tokens"]      = int(m.group(2))
        r["completion_tokens"]  = int(m.group(3))
        r["cache_read_tokens"]  = int(m.group(4))
        r["cache_write_tokens"] = int(m.group(5))
        r["total_tokens"]       = r["prompt_tokens"] + r["completion_tokens"]

    m = CC_REASONING_RE.search(text)
    if m:
        r["reasoning_tokens"] = int(m.group(1))

    m = CC_DURATION_RE.search(text)
    if m:
        r["duration_s"] = float(m.group(1))

    seen: set = set()
    for pattern, label in CC_FAILURE_PATTERNS:
        if label not in seen and pattern.search(text):
            r["loop_failure_reasons"].append(label)
            seen.add(label)

    if r["max_turns_reached"] and "MAX_TURNS_REACHED" not in seen:
        r["loop_failure_reasons"].append("MAX_TURNS_REACHED")
        seen.add("MAX_TURNS_REACHED")

    if not r["completed"] and not r["loop_failure_reasons"]:
        r["loop_failure_reasons"].append("UNKNOWN_INCOMPLETE")

    for tm in CC_TOOL_RE.finditer(text):
        r["tool_counts"][tm.group(1)] += 1

    return r


# ═══════════════════════════════════════════════════════════════════════════════
# Parser — OpenCode
# ═══════════════════════════════════════════════════════════════════════════════

def parse_opencode(text: str) -> Dict:
    r: Dict = {
        "completed":    bool(OC_SUCCESS_RE.search(text)),
        "failed":       bool(OC_FAILED_RE.search(text)),
        "turns":        None,
        "max_turns":    None,
        "max_turns_reached": False,
        "model":        None,
        "loop_failure_reasons": [],
        "tool_counts":  defaultdict(int),
        "cost_usd":            None,
        "prompt_tokens":       None,
        "completion_tokens":   None,
        "cache_read_tokens":   None,
        "cache_write_tokens":  None,
        "reasoning_tokens":    None,
        "total_tokens":        None,
    }

    steps = [int(m.group(1)) for m in OC_STEP_RE.finditer(text)]
    if steps:
        r["turns"] = max(steps)

    m = OC_MODEL_RE.search(text)
    if m:
        r["model"] = m.group(1)

    m = OC_USAGE_RE.search(text)
    if m:
        r["cost_usd"]           = float(m.group(1))
        r["prompt_tokens"]      = int(m.group(2))
        r["completion_tokens"]  = int(m.group(3))
        r["cache_read_tokens"]  = int(m.group(4))
        r["cache_write_tokens"] = int(m.group(5))
        r["reasoning_tokens"]   = int(m.group(6))
        r["total_tokens"]       = int(m.group(7))
        json_totals = None
    else:
        json_totals = _sum_oc_json_events(text)
        if json_totals is not None:
            for k in ("cost_usd", "prompt_tokens", "completion_tokens",
                      "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
                r[k] = json_totals[k]
            r["total_tokens"] = r["prompt_tokens"] + r["completion_tokens"]

    seen: set = set()
    for line in text.splitlines():
        m = OC_ERROR_LINE_RE.match(line.strip())
        if m:
            info  = _parse_oc_error_json(m.group(1))
            label = _oc_status_to_label(info)
            if label and label not in seen:
                r["loop_failure_reasons"].append(label)
                seen.add(label)

    if OC_DOOM_LOOP_RE.search(text) and "DOOM_LOOP" not in seen:
        r["loop_failure_reasons"].append("DOOM_LOOP")
        seen.add("DOOM_LOOP")

    tool_usage_m = OC_TOOL_USAGE_RE.search(text)
    if tool_usage_m:
        # --format json runs: one summary line printed by agent.py (see OC_USAGE_RE above).
        for name, count in OC_TOOL_COUNT_RE.findall(tool_usage_m.group(1)):
            r["tool_counts"][name] += int(count)
    elif json_totals is not None:
        # --format json run without the summary line: counted from tool_use events.
        for name, count in json_totals["tool_counts"].items():
            r["tool_counts"][name] += count
    else:
        # Older text-format runs: tool calls are only visible as pretty-printed arrows.
        for line in text.splitlines():
            line = line.strip()
            m = OC_TOOL_ARROW_RE.match(line)
            if m:
                r["tool_counts"][m.group(1)] += 1
                continue
            if OC_TOOL_BASH_RE.match(line):
                r["tool_counts"]["Bash"] += 1

    # Completion heuristic: if we saw steps and no fatal errors → completed
    if not r["completed"] and not r["failed"]:
        fatal = {"MODEL_NOT_FOUND", "API_ERROR_401"}
        if r["turns"] is not None and not (set(r["loop_failure_reasons"]) & fatal):
            r["completed"] = True

    if not r["completed"] and not r["loop_failure_reasons"]:
        r["loop_failure_reasons"].append("UNKNOWN_INCOMPLETE")

    return r


# ═══════════════════════════════════════════════════════════════════════════════
# Top-level per-file parser
# ═══════════════════════════════════════════════════════════════════════════════

def parse_agent_file(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except Exception:
        return {
            "parse_error": True, "agent_type": "unknown",
            "completed": False, "failed": False,
            "turns": None, "max_turns": None, "max_turns_reached": False,
            "model": None, "loop_failure_reasons": [], "tool_counts": defaultdict(int),
        }

    agent_type = detect_agent_type(text)
    if agent_type == "opencode":
        r = parse_opencode(text)
    else:
        r = parse_claude_code(text)
    r["agent_type"]  = agent_type
    r["parse_error"] = False
    return r


# ═══════════════════════════════════════════════════════════════════════════════
# Report.txt parser — ground truth for pass/fail
# ═══════════════════════════════════════════════════════════════════════════════

PROBLEM_ROW_RE  = re.compile(r'^\|\s*\d+\s*\|\s*([^\|]+?)\s*\|\s*cid\d+.*\|')
PROBLEM_ID_SPLIT_RE = re.compile(r'^(.*)_(\d{4})$')


def parse_report_txt(path: str) -> Tuple[Set[Tuple[str, str]], Set[Tuple[str, str]]]:
    """
    Returns (passing, failing) — each a set of (problem_folder, run_id_str).
    problem_folder  e.g. 'cvdp_agentic_AES_encryption_decryption'
    run_id_str      e.g. '3'   (stripped of leading zeros)
    """
    passing: Set[Tuple[str, str]] = set()
    failing: Set[Tuple[str, str]] = set()

    if not os.path.isfile(path):
        print(f"[WARNING] --report file not found: {path}")
        return passing, failing

    section = None
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if "=== Failing Problems ===" in line:
                section = "failing"
                continue
            if "=== Passing Problems ===" in line:
                section = "passing"
                continue
            if section is None:
                continue
            m = PROBLEM_ROW_RE.match(line)
            if not m:
                continue
            full_id = m.group(1).strip()
            split_m = PROBLEM_ID_SPLIT_RE.match(full_id)
            if split_m:
                problem_folder = split_m.group(1)
                run_id_str     = str(int(split_m.group(2)))   # "0003" → "3"
            else:
                problem_folder = full_id
                run_id_str     = ""
            if section == "failing":
                failing.add((problem_folder, run_id_str))
            else:
                passing.add((problem_folder, run_id_str))

    return passing, failing


# ═══════════════════════════════════════════════════════════════════════════════
# Datapoint failure categorization (only for: loop_ok AND dp_fail)
# ═══════════════════════════════════════════════════════════════════════════════

def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


# --- Commercial EDA (xrun/imc/vmanager) datapoints -----------------------------
# These problems don't go through the icarus rtl/sim.log/result.xml flow below —
# the pytest report at reports/<run_id>.txt is the ground truth instead. Some
# tasks (e.g. SVA writing) don't even generate RTL, so the generic checks would
# misfire on them.

COMMERCIAL_EDA_ERROR_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"xrun\s*:\s*command not found", re.IGNORECASE), "MISSING_XRUN"),
    (re.compile(r"imc\s*:\s*command not found", re.IGNORECASE), "MISSING_IMC"),
    (re.compile(r"vmanager\s*:\s*command not found", re.IGNORECASE), "MISSING_VMANAGER"),
    (re.compile(r"vm_arch\.sh\s*:\s*command not found", re.IGNORECASE), "MISSING_VMANAGER_SCRIPT"),
    (re.compile(r"jre64/bin/java.*[Nn]o such file", re.IGNORECASE), "MISSING_JAVA"),
    (re.compile(r"[Ll]icense checkout failed", re.IGNORECASE), "LICENSE_ERROR"),
    (re.compile(r"Cannot connect to license server", re.IGNORECASE), "LICENSE_ERROR"),
    (re.compile(r"FileNotFoundError.*coverage\.log", re.IGNORECASE), "MISSING_COVERAGE_LOG"),
    (re.compile(r"no space left on device", re.IGNORECASE), "DISK_FULL"),
    (re.compile(r"xrun.*[Ee]rror", re.IGNORECASE), "XRUN_ERROR"),
    (re.compile(r"\*E,\s*\w+", re.IGNORECASE), "XRUN_ELABORATION_ERROR"),
]


def is_commercial_eda_problem(pytest_report_text: str) -> bool:
    """True if this datapoint's pytest report shows a commercial EDA harness (xrun/imc)."""
    return bool(pytest_report_text) and any(
        marker in pytest_report_text
        for marker in ("test_simulate", "test_coverage", "test_report", "xrun", "imc")
    )


def extract_commercial_eda_errors(pytest_report_text: str) -> List[str]:
    """Parse the pytest report file from a commercial EDA harness run."""
    matched_errors: List[str] = []
    seen: set = set()

    for pattern, label in COMMERCIAL_EDA_ERROR_PATTERNS:
        for match in pattern.finditer(pytest_report_text):
            error_key = f"{label}: {match.group(0).strip()}"
            if error_key not in seen:
                seen.add(error_key)
                matched_errors.append(error_key)

    # If test_simulate PASSED but test_report FAILED, coverage collection failed.
    if "test_simulate" in pytest_report_text and "PASSED" in pytest_report_text:
        if "test_report" in pytest_report_text and "FAILED" in pytest_report_text:
            key = "COMMERCIAL_COVERAGE_REPORT_FAILED"
            if key not in seen:
                matched_errors.append(key)

    return matched_errors


def check_datapoint(problem_root: str, run_id: str) -> Dict:
    """
    Categorizes WHY a failing datapoint failed.
    Called only when: loop completed AND report.txt says the datapoint failed.
    """
    rtl_dir     = os.path.join(problem_root, "harness", run_id, "rtl")
    rundir      = os.path.join(problem_root, "harness", run_id, "rundir")
    harness_dir = os.path.join(problem_root, "harness", run_id)
    errors: List[str] = []

    pytest_report_text = _read(os.path.join(problem_root, "reports", f"{run_id}.txt"))
    if is_commercial_eda_problem(pytest_report_text):
        commercial_errors = extract_commercial_eda_errors(pytest_report_text)
        return {"datapoint_errors": commercial_errors or ["NO_COMMERCIAL_ERRORS_DETECTED"]}

    # 1. RTL files present?
    if not os.path.isdir(rtl_dir):
        return {"datapoint_errors": ["NO_RTL_DIR"]}
    rtl_files = [f for f in os.listdir(rtl_dir)
                 if os.path.isfile(os.path.join(rtl_dir, f))]
    if not rtl_files:
        return {"datapoint_errors": ["NO_RTL_CODE"]}
    if all(os.path.getsize(os.path.join(rtl_dir, f)) == 0 for f in rtl_files):
        return {"datapoint_errors": ["RTL_FILE_EMPTY"]}

    # 2. result.xml
    xml_files = glob_mod.glob(os.path.join(rundir, "sim_build", "*.result.xml"))
    if xml_files:
        for xml_path in xml_files:
            try:
                root = ET.parse(xml_path).getroot()
                failure = root.find(".//failure")
                if failure is not None:
                    err_type = failure.attrib.get("error_type", "UnknownError")
                    err_msg  = failure.attrib.get("error_msg", "")
                    errors.append(f"{err_type}: {err_msg}")
            except Exception:
                errors.append("XML_PARSE_ERROR")
    else:
        errors.append("NO_RESULT_XML")

    # 3. sim.log
    sim_log = _read(os.path.join(rundir, "sim.log"))
    if sim_log.strip() == "" and not xml_files:
        errors.append("EMPTY_SIM_LOG")
    elif sim_log:
        for line in sim_log.splitlines():
            low = line.lower()
            if "error" in low or "sorry" in low:
                errors.append(f"SIM_LOG: {line.strip()}")
    elif not xml_files:
        for candidate in [
            os.path.join(harness_dir, "run_docker_harness_direct.log"),
            os.path.join(harness_dir, "docker_compose.log"),
            os.path.join(harness_dir, "docker.log"),
            os.path.join(harness_dir, "pytest.log"),
        ]:
            text = _read(candidate)
            if text:
                for line in text.splitlines():
                    if "error" in line.lower():
                        errors.append(f"HARNESS_LOG: {line.strip()}")
                break
        if not errors:
            errors.append("NO_SIM_LOG")

    # 4. build.log
    build_log = _read(os.path.join(rundir, "build.log"))
    if build_log:
        for line in build_log.splitlines():
            low = line.lower()
            if "error" in low or "syntax error" in low or "elaboration" in low:
                errors.append(f"BUILD_LOG: {line.strip()}")
    else:
        errors.append("NO_BUILD_LOG")

    return {"datapoint_errors": errors}


def categorize_dp_error(error: str) -> str:
    low = error.lower()
    if "no_rtl" in low or "rtl_file_empty" in low:  return "generation_failure"
    if "assertionerror" in low:                       return "functional_failure"
    if "attributeerror" in low or "unable to find the root module" in low:
        return "interface_error"
    if "syntax error" in low or "elaboration" in low or "multiple drivers" in low:
        return "compilation_error"
    if "sorry" in low:                                return "icarus_limitation"
    if "timeout" in low:                              return "timeout"
    if "no_result_xml" in low or "no_sim_log" in low or "empty_sim_log" in low:
        return "pre_simulation_failure"
    if "xml_parse_error" in low or "no_build_log" in low or "no_rtl_dir" in low:
        return "infrastructure"
    if "cannot convert logic" in low or "non-0/1" in low:
        return "runtime_signal_error"

    # --- commercial EDA specific ---
    if any(k in low for k in ("missing_xrun", "missing_imc", "missing_vmanager",
                               "missing_vmanager_script", "missing_java")):
        return "infrastructure"
    if "license_error" in low:
        return "infrastructure"
    if "disk_full" in low:
        return "infrastructure"
    if "missing_coverage_log" in low or "commercial_coverage_report_failed" in low or "no_commercial_errors_detected" in low:
        return "infrastructure"
    if "xrun_elaboration_error" in low or "xrun_error" in low:
        return "compilation_error"

    return "unknown"


# ═══════════════════════════════════════════════════════════════════════════════
# Ground truth from report.txt
# ═══════════════════════════════════════════════════════════════════════════════

passing_set: Set[Tuple[str, str]] = set()
failing_set: Set[Tuple[str, str]] = set()
have_report = os.path.isfile(report_path)

if have_report:
    passing_set, failing_set = parse_report_txt(report_path)
    print(f"Report: {len(passing_set)} passing, {len(failing_set)} failing datapoints")

# (problem, run_id) pairs actually named in the report — only these are mined.
known_keys: Set[Tuple[str, str]] = passing_set | failing_set


# ═══════════════════════════════════════════════════════════════════════════════
# Discover *_agent.txt files (restricted to datapoints named in the report)
# ═══════════════════════════════════════════════════════════════════════════════

AGENT_FILE_RE = re.compile(r"^(\d+)_agent\.txt$")

entries = []
skipped_not_in_report = 0
try:
    problem_dirs = sorted(os.listdir(work_dir))
except FileNotFoundError:
    print(f"[ERROR] work_dir not found: {work_dir}")
    raise SystemExit(1)

for problem in problem_dirs:
    reports_dir = os.path.join(work_dir, problem, "reports")
    if not os.path.isdir(reports_dir):
        continue
    for fname in sorted(os.listdir(reports_dir)):
        m = AGENT_FILE_RE.match(fname)
        if not m:
            continue
        run_id = m.group(1)
        if known_keys and (problem, str(int(run_id))) not in known_keys:
            skipped_not_in_report += 1
            continue
        entries.append({
            "problem": problem,
            "run_id":  run_id,
            "path":    os.path.join(reports_dir, fname),
        })

print(f"Found {len(entries)} agent report(s) in '{work_dir}'"
      + (f"  ({skipped_not_in_report} skipped — not listed in report)"
         if skipped_not_in_report else ""))

# ═══════════════════════════════════════════════════════════════════════════════
# Build records
# ═══════════════════════════════════════════════════════════════════════════════

records = []
for e in entries:
    r = parse_agent_file(e["path"])
    r["problem"] = e["problem"]
    r["run_id"]  = e["run_id"]

    key          = (e["problem"], str(int(e["run_id"])))
    problem_root = os.path.join(work_dir, e["problem"])

    if have_report:
        if key in passing_set:
            r["datapoint_outcome"]    = "pass"
            r["datapoint_errors"]     = []
            r["datapoint_categories"] = []
        elif key in failing_set:
            r["datapoint_outcome"] = "fail"
            if r["completed"]:
                # Agent completed but RTL failed — go find out why
                dp = check_datapoint(problem_root, e["run_id"])
                r["datapoint_errors"]     = dp["datapoint_errors"]
                r["datapoint_categories"] = list(dict.fromkeys(
                    categorize_dp_error(err) for err in dp["datapoint_errors"]
                ))
            else:
                # Agent itself failed → RTL failure is a consequence, not the cause
                r["datapoint_errors"]     = []
                r["datapoint_categories"] = []
        else:
            r["datapoint_outcome"]    = "unknown"
            r["datapoint_errors"]     = []
            r["datapoint_categories"] = []
    else:
        r["datapoint_outcome"]    = "unknown"
        r["datapoint_errors"]     = []
        r["datapoint_categories"] = []

    records.append(r)

# ═══════════════════════════════════════════════════════════════════════════════
# Aggregates
# ═══════════════════════════════════════════════════════════════════════════════

total        = len(records)
n_loop_ok    = sum(1 for r in records if r["completed"])
n_loop_fail  = total - n_loop_ok
n_max_turns  = sum(1 for r in records if r["max_turns_reached"])
n_dp_pass    = sum(1 for r in records if r["datapoint_outcome"] == "pass")
n_dp_fail    = sum(1 for r in records if r["datapoint_outcome"] == "fail")
# among completed loops: how many passed / failed at the datapoint level
n_loop_ok_dp_pass = sum(1 for r in records
                        if r["completed"] and r["datapoint_outcome"] == "pass")
n_loop_ok_dp_fail = sum(1 for r in records
                        if r["completed"] and r["datapoint_outcome"] == "fail")
loop_ok_dp_known  = n_loop_ok_dp_pass + n_loop_ok_dp_fail  # excludes "unknown"

agent_type_counter: Dict[str, int] = defaultdict(int)
for r in records:
    agent_type_counter[r["agent_type"]] += 1

loop_reason_counter: Dict[str, int] = defaultdict(int)
for r in records:
    if not r["completed"]:
        for reason in set(r["loop_failure_reasons"]):
            loop_reason_counter[reason] += 1

dp_cat_counter: Dict[str, int] = defaultdict(int)
for r in records:
    if r["completed"] and r["datapoint_outcome"] == "fail":
        for cat in set(r["datapoint_categories"]):
            dp_cat_counter[cat] += 1

tool_totals: Dict[str, int] = defaultdict(int)
for r in records:
    for tool, count in r["tool_counts"].items():
        tool_totals[tool] += count

turns_list = [r["turns"] for r in records if r["turns"] is not None]

# Cost/token accounting — populated for opencode (usage_summary line) and claude_code (the [result] cost=/input=/output=... line).
cost_list           = [r["cost_usd"]           for r in records if r.get("cost_usd")           is not None]
total_tokens_list   = [r["total_tokens"]       for r in records if r.get("total_tokens")       is not None]
prompt_tokens_list  = [r["prompt_tokens"]      for r in records if r.get("prompt_tokens")      is not None]
completion_tokens_list = [r["completion_tokens"] for r in records if r.get("completion_tokens") is not None]
cache_read_list     = [r["cache_read_tokens"]  for r in records if r.get("cache_read_tokens")  is not None]
cache_write_list    = [r["cache_write_tokens"] for r in records if r.get("cache_write_tokens") is not None]
reasoning_list      = [r["reasoning_tokens"]   for r in records if r.get("reasoning_tokens")   is not None]
# Agent wall-clock time — only codex prints it (duration= on the [result] line).
duration_list       = [r["duration_s"]         for r in records if r.get("duration_s")         is not None]

# ═══════════════════════════════════════════════════════════════════════════════
# Output 1: agent_runs.csv
# ═══════════════════════════════════════════════════════════════════════════════

with open(os.path.join(out_dir, "agent_runs.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow([
        "problem", "run_id", "agent_type",
        "loop_outcome", "turns", "max_turns", "max_turns_reached", "model",
        "loop_failure_reasons",
        "cost_usd", "prompt_tokens", "completion_tokens",
        "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "total_tokens",
        "duration_s",
        "datapoint_outcome", "datapoint_categories", "datapoint_errors",
    ])
    for r in records:
        loop_outcome = ("success"    if r["completed"]
                        else "failed" if r["failed"]
                        else "incomplete")
        w.writerow([
            r["problem"], r["run_id"], r["agent_type"],
            loop_outcome,
            r["turns"]     if r["turns"]     is not None else "",
            r["max_turns"] if r["max_turns"] is not None else "",
            r["max_turns_reached"],
            r.get("model") or "",
            "|".join(r["loop_failure_reasons"]),
            r.get("cost_usd")           if r.get("cost_usd")           is not None else "",
            r.get("prompt_tokens")      if r.get("prompt_tokens")      is not None else "",
            r.get("completion_tokens")  if r.get("completion_tokens")  is not None else "",
            r.get("cache_read_tokens")  if r.get("cache_read_tokens")  is not None else "",
            r.get("cache_write_tokens") if r.get("cache_write_tokens") is not None else "",
            r.get("reasoning_tokens")   if r.get("reasoning_tokens")   is not None else "",
            r.get("total_tokens")       if r.get("total_tokens")       is not None else "",
            r.get("duration_s")         if r.get("duration_s")         is not None else "",
            r["datapoint_outcome"],
            "|".join(r["datapoint_categories"]),
            " ||| ".join(r["datapoint_errors"]),
        ])

# ═══════════════════════════════════════════════════════════════════════════════
# Output 2: agent_failure_reasons.csv
# ═══════════════════════════════════════════════════════════════════════════════

with open(os.path.join(out_dir, "agent_failure_reasons.csv"), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(["problem", "run_id", "agent_type", "level", "reason_or_category"])
    for r in records:
        at = r["agent_type"]
        if r["loop_failure_reasons"]:
            for reason in r["loop_failure_reasons"]:
                w.writerow([r["problem"], r["run_id"], at, "loop", reason])
        elif r["completed"]:
            w.writerow([r["problem"], r["run_id"], at, "loop", ""])
        if r["completed"] and r["datapoint_outcome"] != "unknown":
            if r["datapoint_categories"]:
                for cat in r["datapoint_categories"]:
                    w.writerow([r["problem"], r["run_id"], at, "datapoint", cat])
            else:
                w.writerow([r["problem"], r["run_id"], at, "datapoint",
                            r["datapoint_outcome"]])

# ═══════════════════════════════════════════════════════════════════════════════
# Output 3: agent_summary.txt
# ═══════════════════════════════════════════════════════════════════════════════

with open(os.path.join(out_dir, "agent_summary.txt"), "w", encoding="utf-8") as fh:
    fh.write("Agentic Run Summary\n")
    fh.write("===================\n\n")
    fh.write(f"Total runs:                   {total}\n")
    for atype, count in sorted(agent_type_counter.items()):
        fh.write(f"  [{atype}]  {count}\n")
    if total:
        fh.write(f"\nLoop completed OK:            {n_loop_ok}  ({100*n_loop_ok/total:.1f}%)\n")
        fh.write(f"Loop failed / incomplete:     {n_loop_fail}\n")
        fh.write(f"Max turns reached:            {n_max_turns}\n")
    if turns_list:
        fh.write(f"\nTurns — avg: {sum(turns_list)/len(turns_list):.1f}  "
                 f"min: {min(turns_list)}  max: {max(turns_list)}\n")

    if cost_list:
        fh.write("\n── Cost & Token Usage (opencode + claude_code) ─────────\n")
        fh.write(f"  Runs with cost data:         {len(cost_list)}\n")
        fh.write(f"  Total cost:                  ${sum(cost_list):.2f}\n")
        fh.write(f"  Avg cost / run:               ${sum(cost_list)/len(cost_list):.4f}\n")
        fh.write(f"  Min / max cost / run:         ${min(cost_list):.4f} / ${max(cost_list):.4f}\n")
        if total_tokens_list:
            fh.write(f"  Avg total tokens / run:       {sum(total_tokens_list)/len(total_tokens_list):,.0f}\n")
        if prompt_tokens_list:
            fh.write(f"  Total prompt tokens:          {sum(prompt_tokens_list):,}\n")
        if completion_tokens_list:
            fh.write(f"  Total completion tokens:      {sum(completion_tokens_list):,}\n")
        if cache_read_list:
            fh.write(f"  Total cache-read tokens:      {sum(cache_read_list):,}\n")
        if cache_write_list:
            fh.write(f"  Total cache-write tokens:     {sum(cache_write_list):,}\n")
        if reasoning_list:
            fh.write(f"  Total reasoning tokens:       {sum(reasoning_list):,}\n")

    if duration_list:
        fh.write("\n── Agent Time (codex) ────────────────────────────────────────────\n")
        fh.write(f"  Runs with time data:         {len(duration_list)}\n")
        fh.write(f"  Total time:                  {sum(duration_list)/60:.1f} min\n")
        fh.write(f"  Avg time / run:               {sum(duration_list)/len(duration_list):.1f} s\n")
        fh.write(f"  Min / max time / run:         {min(duration_list):.1f} s / {max(duration_list):.1f} s\n")

    fh.write("\n── Loop Failure Reasons ──────────────────────────────────────────\n")
    for reason, count in sorted(loop_reason_counter.items(),
                                key=lambda x: x[1], reverse=True):
        fh.write(f"  {count:4d}  {reason}\n")
    if not loop_reason_counter:
        fh.write("  (none)\n")

    if have_report:
        fh.write(f"\n── Datapoint Outcomes  (from report.txt) ─────────────────────────\n")
        fh.write(f"  {n_dp_pass:4d}  pass  (overall)\n")
        fh.write(f"  {n_dp_fail:4d}  fail  (overall)\n")
        fh.write(f"\n── Pass rate: loop completed only ────────────────────────────────\n")
        if loop_ok_dp_known:
            pr = 100 * n_loop_ok_dp_pass / loop_ok_dp_known
            fh.write(f"  {n_loop_ok_dp_pass} / {loop_ok_dp_known}  =  {pr:.1f}%"
                     f"  (pass / evaluated runs where loop_ok)\n")
        else:
            fh.write("  n/a\n")

        fh.write("\n── Failure breakdown: loop_ok + datapoint_fail ───────────────────\n")
        fh.write("   (why did the agent complete but RTL still fail?)\n\n")
        for cat, count in sorted(dp_cat_counter.items(),
                                 key=lambda x: x[1], reverse=True):
            fh.write(f"  {count:4d}  {cat}\n")
        if not dp_cat_counter:
            fh.write("  (none — all completed runs passed)\n")
    else:
        fh.write("\n[NOTE] No --report provided: datapoint pass/fail unknown.\n")
        fh.write("       Run with --report <path/to/report.txt> for full analysis.\n")

    fh.write("\n── Tool Usage Totals ─────────────────────────────────────────────\n")
    for tool, count in sorted(tool_totals.items(), key=lambda x: x[1], reverse=True):
        fh.write(f"  {count:6d}  {tool}\n")

# ── Console ───────────────────────────────────────────────────────────────────

print(f"\nAgent types: {dict(agent_type_counter)}")
print(f"Loop:      {n_loop_ok} completed / {n_loop_fail} failed / {total} total")
if n_max_turns:
    print(f"Max turns hit: {n_max_turns}")
if turns_list:
    print(f"Turns avg/max: {sum(turns_list)/len(turns_list):.1f} / {max(turns_list)}")
if cost_list:
    print(f"Cost: ${sum(cost_list):.2f} total / ${sum(cost_list)/len(cost_list):.4f} avg per run ({len(cost_list)} runs with cost data)")
if duration_list:
    print(f"Agent time: {sum(duration_list)/60:.1f} min total / {sum(duration_list)/len(duration_list):.1f} s avg per run ({len(duration_list)} runs with time data)")

if have_report:
    print(f"Datapoint: {n_dp_pass} pass / {n_dp_fail} fail  (ground truth from report.txt)")
    if loop_ok_dp_known:
        pr = 100 * n_loop_ok_dp_pass / loop_ok_dp_known
        print(f"  Pass rate (loop_ok only): {n_loop_ok_dp_pass}/{loop_ok_dp_known} = {pr:.1f}%")
    print(f"  └ loop_ok + dp_fail: {n_loop_ok_dp_fail}  (agent completed, RTL wrong)")
    if dp_cat_counter:
        print("\nDatapoint failure categories (agent completed, dp failed):")
        for cat, count in sorted(dp_cat_counter.items(),
                                 key=lambda x: x[1], reverse=True)[:8]:
            print(f"  {count:4d}  {cat}")
else:
    print("Datapoint pass/fail: unknown (use --report to provide ground truth)")

if loop_reason_counter:
    print("\nLoop failure reasons:")
    for reason, count in sorted(loop_reason_counter.items(),
                                key=lambda x: x[1], reverse=True)[:8]:
        print(f"  {count:4d}  {reason}")

print(f"\nOutputs written to '{out_dir}/':")
print(f"  agent_runs.csv            — one row per run, both levels")
print(f"  agent_failure_reasons.csv — one row per (run, reason), for pivot")
print(f"  agent_summary.txt         — human-readable aggregate")