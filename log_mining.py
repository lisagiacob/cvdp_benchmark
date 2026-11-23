from collections import defaultdict
import re
import glob
import xml.etree.ElementTree as ET
import csv
import os
from typing import List, Tuple
import argparse


errors_by_id = defaultdict(set)
error_counter = defaultdict(int)
processed_problem_ids = set()
skipped_problem_ids = set()

parser = argparse.ArgumentParser()
parser.add_argument("report_path", nargs="?", default="work/report.txt")
args = parser.parse_args()
report_path = args.report_path

failed_problems = []
	
with open(report_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

in_failing_section = False
current_problem_id = None

problem_row_re = re.compile(
    r'^\|\s*\d+\s*\|\s*([^\|]+?)\s*\|\s*cid\d+.*\|'
)

log_row_re = re.compile(
    r'^\|\s*\|\s*↳\s*Test\s+\d+:\s*[^\|]+?\s*\|.*Log:\s*(.+?)\s*\|$'
)

# This regex will be used to split the problem ID into a base name and a numeric suffix, if it exists.
problem_id_split_re = re.compile(r'^(.*)_(\d{4})$')

HARNESS_ERROR_PATTERNS = [
    (re.compile(r"FreeUsageLimitError", re.IGNORECASE), "API_RATE_LIMIT"),
    (re.compile(r"Rate limit exceeded", re.IGNORECASE), "API_RATE_LIMIT"),
    (re.compile(r"statusCode[\"']?\s*:\s*429", re.IGNORECASE), "API_RATE_LIMIT"),
    (re.compile(r"\b429\b", re.IGNORECASE), "API_RATE_LIMIT"),
    (re.compile(r"ModuleNotFoundError", re.IGNORECASE), "TEST_RUNNER_IMPORT_ERROR"),
    (re.compile(r"ImportError while importing test module", re.IGNORECASE), "TEST_RUNNER_IMPORT_ERROR"),
    (re.compile(r"No module named ['\"]cocotb_tools['\"]", re.IGNORECASE), "MISSING_COCOTB_TOOLS"),
    (re.compile(r"No module named ['\"]([^'\"]+)['\"]", re.IGNORECASE), "MISSING_PYTHON_MODULE"),
    (re.compile(r"PytestCacheWarning", re.IGNORECASE), "PYTEST_CACHE_WARNING"),
    (re.compile(r"Permission denied", re.IGNORECASE), "PERMISSION_DENIED"),
]


def read_text_file_if_exists(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def collect_harness_debug_sources(
    problem_root: str,
    run_id: str,
    report_path: str,
    agent_report_path: str = "",
) -> List[Tuple[str, str]]:
    sources: List[Tuple[str, str]] = []

    harness_dir = os.path.join(problem_root, "harness", run_id)
    reports_dir = os.path.join(problem_root, "reports")

    candidate_paths = [
        report_path,
        agent_report_path,
        os.path.join(harness_dir, "run_docker_harness_direct.log"),
        os.path.join(harness_dir, "docker_compose.log"),
        os.path.join(harness_dir, "docker-compose.log"),
        os.path.join(harness_dir, "docker.log"),
        os.path.join(harness_dir, "pytest.log"),
    ]

    for candidate in candidate_paths:
        text = read_text_file_if_exists(candidate)
        if text:
            sources.append((candidate, text))

    return sources


def extract_harness_errors(
    problem_root: str,
    run_id: str,
    report_path: str,
    agent_report_path: str = "",
) -> List[str]:
    matched_errors = []
    seen = set()

    for source_path, text in collect_harness_debug_sources(
        problem_root, run_id, report_path, agent_report_path
    ):
        for pattern, label in HARNESS_ERROR_PATTERNS:
            for match in pattern.finditer(text):
                if label == "MISSING_PYTHON_MODULE":
                    module_name = match.group(1)
                    error_key = f"{label}: {module_name} [source={os.path.basename(source_path)}]"
                elif label == "TEST_RUNNER_IMPORT_ERROR":
                    line = match.group(0).strip()
                    error_key = f"{label}: {line} [source={os.path.basename(source_path)}]"
                else:
                    line = match.group(0).strip()
                    error_key = f"{label}: {line} [source={os.path.basename(source_path)}]"

                if error_key not in seen:
                    seen.add(error_key)
                    matched_errors.append(error_key)

    return matched_errors


def categorize_error(error_type: str, report_path: str = "") -> str:
    error_lower = error_type.lower()
    report_lower = report_path.lower()

    if "api_rate_limit" in error_lower or "freeusagelimiterror" in error_lower or "429" in error_lower or "rate limit exceeded" in error_lower or "too many requests" in error_lower:
        return "api_error"

    if "429" in error_lower or "too many requests" in error_lower:
        return "api_error"

    # --- generation / artifact creation failures ---
    if "no_rtl_code" in error_lower:
        return "generation_failure"

    if "rtl_file_empty" in error_lower:
        return "generation_failure"

    if "no_rtl_dir" in error_lower:
        return "infrastructure_error"

    if "test_runner_import_error" in error_lower:
        return "test_runner_failure"

    if "missing_cocotb_tools" in error_lower:
        return "test_runner_failure"

    if "missing_python_module" in error_lower:
        return "test_runner_failure"

    if "pytest_cache_warning" in error_lower:
        return "infrastructure_error"

    if "permission_denied" in error_lower:
        return "infrastructure_error"

    # --- missing runtime artifacts ---
    if "no_sim_log" in error_lower:
        if "_lint.txt" in report_lower:
            return "compilation_error"
        if "_sanity.txt" in report_lower:
            return "interface_error"
        return "pre_simulation_failure"

    if "empty_sim_log" in error_lower:
        return "pre_simulation_failure"

    if "no_result_xml" in error_lower:
        return "pre_simulation_failure"

    if "xml_parse_error" in error_lower:
        return "infrastructure_error"

    # --- runtime value / X propagation issues ---
    if "cannot convert logic('x') to int" in error_lower:
        return "runtime_signal_error"

    if "logicarray" in error_lower or "non-0/1 values" in error_lower:
        return "runtime_signal_error"

    # --- functional mismatches caught by the testbench ---
    if "assertionerror" in error_lower:
        return "functional_failure"

    # --- DUT / TB interface mismatches ---
    if "attributeerror" in error_lower:
        return "interface_error"

    if "unable to find the root module" in error_lower:
        return "interface_error"

    if "packed objects" in error_lower and "cannot be indexed" in error_lower:
        return "interface_error"

    # --- compilation / elaboration errors ---
    if "syntax error" in error_lower:
        return "compilation_error"

    if "multiple drivers" in error_lower:
        return "compilation_error"

    if "not a valid l-value for a procedural assignment" in error_lower:
        return "compilation_error"

    if "needs an array index here" in error_lower:
        return "compilation_error"

    if "unable to elaborate" in error_lower:
        return "compilation_error"

    if "error(s) during elaboration" in error_lower:
        return "compilation_error"

    if "elaboration failed" in error_lower:
        return "compilation_error"

    if "sim_log:" in error_lower and "error" in error_lower:
        return "compilation_error"

    # --- simulator limitations / warnings ---
    if "sim_log:" in error_lower and "sorry" in error_lower:
        return "icarus_error"

    if "timeout" in error_lower:
        return "timeout"

    if "systemexit" in error_lower:
        return "test_runner_failure"

    return "unknown"


def categorize_blame(category: str) -> str:
    if category in {
        "compilation_error",
        "interface_error",
        "runtime_signal_error",
        "functional_failure",
        "timeout",
    }:
        return "LLM"

    if category in {"generation_failure", "pre_simulation_failure", "unknown"}:
        return "unknown"

    if category in {"icarus_error"}:
        return "tool_failure"

    if category in {"api_error", "infrastructure_error", "test_runner_failure"}:
        return "integration"

    return "unknown"


def extract_agent_errors(agent_report_path: str) -> List[str]:
    matched_errors = []
    seen = set()

    text = read_text_file_if_exists(agent_report_path)
    if not text:
        return matched_errors

    for pattern, label in HARNESS_ERROR_PATTERNS:
        for match in pattern.finditer(text):
            if label == "MISSING_PYTHON_MODULE":
                module_name = match.group(1)
                error_key = f"{label}: {module_name} [source={os.path.basename(agent_report_path)}]"
            else:
                line = match.group(0).strip()
                error_key = f"{label}: {line} [source={os.path.basename(agent_report_path)}]"

            if error_key not in seen:
                seen.add(error_key)
                matched_errors.append(error_key)

    return matched_errors

# Helper functions for error reporting
def is_secondary_artifact_error(error_type: str) -> bool:
    error_lower = error_type.lower()
    return error_lower in {"no_result_xml", "no_sim_log", "empty_sim_log"}


def select_primary_errors_for_reporting(errors, report_path: str):
    errors = set(errors)
    categories = {categorize_error(error, report_path) for error in errors}

    has_stronger_root_cause = any(
        category in {
            "test_runner_failure",
            "compilation_error",
            "interface_error",
            "runtime_signal_error",
            "functional_failure",
            "icarus_error",
            "timeout",
            "api_error",
            "infrastructure_error",
            "generation_failure",
        }
        for category in categories
    )

    if not has_stronger_root_cause:
        return sorted(errors)

    filtered_errors = [error for error in errors if not is_secondary_artifact_error(error)]

    if filtered_errors:
        return sorted(filtered_errors)

    return sorted(errors)


for line in lines:
    if "=== Failing Problems ===" in line:
        in_failing_section = True
        continue
    
    # Stop b4 the passing problems section
    if in_failing_section and "=== Passing Problems ===" in line:
        in_failing_section = False
        break

    # Only process lines in the failing section
    if not in_failing_section:  
        continue

    problem_match = problem_row_re.match(line)
    # If we find a problem row, update the current problem ID
    if problem_match:
        current_problem_id = problem_match.group(1).strip()
        continue

    # If we find a log row, extract the test problem ID and log path, and add to the failed problems list
    log_match = log_row_re.match(line)
    if log_match:
        report_path = os.path.abspath(log_match.group(1).strip())
        #report_path = report_path.replace("/work/", "/PrevWork/")

        # Try to split the problem ID into a base name and a numeric suffix (run ID). 
        # If it doesn't match the pattern, use the full problem ID as the base and set run_id to None.
        split_match = problem_id_split_re.match(current_problem_id)
        if split_match:
            problem_base = split_match.group(1)
            run_id = str(int(split_match.group(2)))   # 0012 -> 12
        else:
            problem_base = current_problem_id
            run_id = None

        failed_problems.append(
            {
                "full_problem_id": current_problem_id,
                "problem_base": problem_base,
                "run_id": run_id,
                "report_path": report_path,
                "agent_report_path": (
                    os.path.join(
                        os.path.dirname(os.path.dirname(report_path)),
                        "reports",
                        f"{run_id}_agent.txt",
                    )
                    if run_id is not None else ""
                ),
            }
        )

    
for problem in failed_problems:
#    print(problem)

# Output example un until now:
# {'problem_id': 'cvdp_copilot_16qam_mapper_0001', 'test_index': '1', 'report_path': '/home/giacobazzi/cvdp/cvdp_benchmark/work/cvdp_copilot_16qam_mapper/reports/1.txt'}

## Let's start analysing the logs to find the error types.

    problem_id = problem["full_problem_id"]

    if problem_id in processed_problem_ids:
        continue
    processed_problem_ids.add(problem_id)

    problem_root = os.path.dirname(os.path.dirname(problem["report_path"]))
    rundir = os.path.join(problem_root, "harness", problem["run_id"], "rundir")

    # crea comunque la entry, anche se poi non trovi errori
    errors_by_id[problem_id]

    agent_errors = extract_agent_errors(problem.get("agent_report_path", ""))
    for agent_error in agent_errors:
        if agent_error not in errors_by_id[problem_id]:
            errors_by_id[problem_id].add(agent_error)
            error_counter[agent_error] += 1
    
    if any(e.startswith("API_RATE_LIMIT") for e in errors_by_id[problem_id]):
        skipped_problem_ids.add(problem_id)
    #    continue

    # --- Check if RTL code was generated ---
    rtl_dir = os.path.join(problem_root, "harness", problem["run_id"], "rtl")
    #print(f"Checking RTL for {problem_id} in {rtl_dir}")
    if os.path.isdir(rtl_dir):
        rtl_files = [f for f in os.listdir(rtl_dir) if os.path.isfile(os.path.join(rtl_dir, f))]

        if len(rtl_files) == 0:
            if "NO_RTL_CODE" not in errors_by_id[problem_id]:
                errors_by_id[problem_id].add("NO_RTL_CODE")
                error_counter["NO_RTL_CODE"] += 1
                continue
        else:
            empty_rtl_files = []
            for rtl_file in rtl_files:
                rtl_path = os.path.join(rtl_dir, rtl_file)
                if os.path.getsize(rtl_path) == 0:
                    empty_rtl_files.append(rtl_file)

            if len(empty_rtl_files) == len(rtl_files):
                if "RTL_FILE_EMPTY" not in errors_by_id[problem_id]:
                    errors_by_id[problem_id].add("RTL_FILE_EMPTY")
                    error_counter["RTL_FILE_EMPTY"] += 1
                    continue
    else:
        if "NO_RTL_DIR" not in errors_by_id[problem_id]:
            errors_by_id[problem_id].add("NO_RTL_DIR")
            error_counter["NO_RTL_DIR"] += 1
            continue

    # --- 1. Analizza sim.log ---
    sim_log_path = f"{rundir}/sim.log"

    try:
        with open(sim_log_path, "r", encoding="utf-8") as f:
            sim_log = f.read()

        if sim_log.strip() == "":
            if "EMPTY_SIM_LOG" not in errors_by_id[problem_id]:
                errors_by_id[problem_id].add("EMPTY_SIM_LOG")
                error_counter["EMPTY_SIM_LOG"] += 1
        else:
            # per ora salviamo le righe che contengono "error"
            for line in sim_log.splitlines():
                if "error" in line.lower():
                    error_key = f"SIM_LOG: {line.strip()}"
                    if error_key not in errors_by_id[problem_id]:
                        errors_by_id[problem_id].add(error_key)
                        error_counter[error_key] += 1
                elif "sorry" in line.lower():
                    error_key = f"SIM_LOG: {line.strip()}"
                    if error_key not in errors_by_id[problem_id]:
                        errors_by_id[problem_id].add(error_key)
                        error_counter[error_key] += 1

    except FileNotFoundError:
        harness_errors = extract_harness_errors(
            problem_root,
            problem["run_id"],
            problem["report_path"],
            problem.get("agent_report_path", ""),
        )

        if harness_errors:
            for harness_error in harness_errors:
                if harness_error not in errors_by_id[problem_id]:
                    errors_by_id[problem_id].add(harness_error)
                    error_counter[harness_error] += 1
        else:
            if "NO_SIM_LOG" not in errors_by_id[problem_id]:
                errors_by_id[problem_id].add("NO_SIM_LOG")
                error_counter["NO_SIM_LOG"] += 1
#        continue

#    if "NO_SIM_LOG" in errors_by_id[problem_id]:
#        continue
    
    # --- 2. Analizza result.xml ---
    xml_files = glob.glob(f"{rundir}/sim_build/*.result.xml")

    if not xml_files:
        if "NO_RESULT_XML" not in errors_by_id[problem_id]:
            errors_by_id[problem_id].add("NO_RESULT_XML")
            error_counter["NO_RESULT_XML"] += 1
    #    continue

    for xml_path in xml_files:
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()

            failure = root.find(".//failure")

            if failure is not None:
                err_type = failure.attrib.get("error_type", "UnknownError")
                err_msg = failure.attrib.get("error_msg", "")
                error_key = f"{err_type}: {err_msg}"

                if error_key not in errors_by_id[problem_id]:
                    errors_by_id[problem_id].add(error_key)
                    error_counter[error_key] += 1

        except Exception:
            if "XML_PARSE_ERROR" not in errors_by_id[problem_id]:
                errors_by_id[problem_id].add("XML_PARSE_ERROR")
                error_counter["XML_PARSE_ERROR"] += 1

    # --- 3. Analizza build.log ---
    build_log_path = os.path.join(rundir, "build.log")

    if os.path.isfile(build_log_path):
        try:
            with open(build_log_path, "r", encoding="utf-8") as f:
                build_log = f.read()

            for line in build_log.splitlines():
                low = line.lower()
                if "error" in low or "syntax error" in low or "elaboration" in low or "unknown module type" in low:
                    error_key = f"BUILD_LOG: {line.strip()}"
                    if error_key not in errors_by_id[problem_id]:
                        errors_by_id[problem_id].add(error_key)
                        error_counter[error_key] += 1
        except Exception:
            if "BUILD_PARSE_ERROR" not in errors_by_id[problem_id]:
                errors_by_id[problem_id].add("BUILD_PARSE_ERROR")
                error_counter["BUILD_PARSE_ERROR"] += 1
    else:
        if "NO_BUILD_LOG" not in errors_by_id[problem_id]:
            errors_by_id[problem_id].add("NO_BUILD_LOG")
            error_counter["NO_BUILD_LOG"] += 1

with open("Logs/harness_debug_summary.txt", "w", encoding="utf-8") as f:
    f.write("Harness Debug Summary\n")
    f.write("=====================\n\n")

    for problem in failed_problems:
        problem_id = problem["full_problem_id"]
        harness_errors = [e for e in sorted(errors_by_id[problem_id]) if (
            e.startswith("TEST_RUNNER_IMPORT_ERROR")
            or e.startswith("MISSING_COCOTB_TOOLS")
            or e.startswith("MISSING_PYTHON_MODULE")
            or e.startswith("PYTEST_CACHE_WARNING")
            or e.startswith("PERMISSION_DENIED")
            or e.startswith("API_RATE_LIMIT")
        )]

        if harness_errors:
            f.write(f"{problem_id}\n")
            for err in harness_errors:
                f.write(f"  - {err}\n")
            f.write("\n")

print("failed_test =", len(failed_problems))
effective_failed = [pid for pid in errors_by_id if pid not in skipped_problem_ids]

print("failed_problems =", len(effective_failed))
print("skipped_problems =", len(skipped_problem_ids))

with open("Logs/error_summary.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["error_type", "count"])

    for error, count in sorted(error_counter.items(), key=lambda x: x[1], reverse=True):
        writer.writerow([error, count])

category_counter = defaultdict(int)
blame_counter = defaultdict(int)

with open("Logs/error_categories_by_problem.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["problem_id", "error_message", "category", "probable_blame"])

    report_path_by_problem = {}
    for problem in failed_problems:
        problem_id = problem["full_problem_id"]
        if problem_id not in report_path_by_problem:
            report_path_by_problem[problem_id] = problem["report_path"]

    for problem_id, errors in errors_by_id.items():
        if problem_id in skipped_problem_ids:
            continue
        report_path = report_path_by_problem.get(problem_id, "")
        primary_errors = select_primary_errors_for_reporting(errors, report_path)

        if not primary_errors:
            category = "unknown"
            blame = "unknown"
            writer.writerow([problem_id, "NO_ERRORS_COLLECTED", category, blame])

            category_counter[category] += 1
            blame_counter[blame] += 1

            continue

        seen_categories = set()
        seen_blames = set()

        for error in primary_errors:
            category = categorize_error(error, report_path)
            blame = categorize_blame(category)
            writer.writerow([problem_id, error, category, blame])

            if category not in seen_categories:
                category_counter[category] += 1
                seen_categories.add(category)

            if blame not in seen_blames:
                blame_counter[blame] += 1
                seen_blames.add(blame)


with open("Logs/error_category_summary.txt", "w", encoding="utf-8") as f:
    f.write("Error Category Summary\n")
    f.write("======================\n\n")

    for category, count in sorted(category_counter.items(), key=lambda x: x[1], reverse=True):
        f.write(f"{count:4d}  {category}\n")

    f.write("\nProbable Blame Summary\n")
    f.write("======================\n\n")

    for blame, count in sorted(blame_counter.items(), key=lambda x: x[1], reverse=True):
        f.write(f"{count:4d}  {blame}\n")


with open("Logs/error_blame_summary.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["probable_blame", "count"])

    for blame, count in sorted(blame_counter.items(), key=lambda x: x[1], reverse=True):
        writer.writerow([blame, count])
