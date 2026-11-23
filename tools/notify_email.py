#!/usr/bin/env python3
"""Send an email notification when a `make` benchmark run finishes.

Uses the server's local MTA via the `mail` command. Configuration is read
from environment variables (see .env.example):
  NOTIFY_EMAIL_TO     recipient address (required to actually send)

If NOTIFY_EMAIL_TO is unset, the script prints a warning and exits 0 so it
never breaks a benchmark run just because email isn't set up.

With --prefix, the body also reports the problem pass rate from
<prefix>/raw_result.json (and, for --iter-to-convergence runs, how many
problems passed at the first agent exit and how many converged).
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()


def problem_summary(prefix: str) -> str:
    """Pass-rate lines for the email body; empty if the run left no results."""
    try:
        with open(os.path.join(prefix, "raw_result.json")) as f:
            results = json.load(f)
    except (OSError, ValueError):
        return f"Results: no readable {prefix}/raw_result.json\n"
    total = len(results)
    if not total:
        return "Results: raw_result.json is empty\n"

    passed = sum(1 for r in results.values() if r.get("status") == "passed")
    skipped = sum(1 for r in results.values() if r.get("status") == "skipped")
    lines = f"Problem pass rate: {passed}/{total} ({100 * passed / total:.2f}%)\n"
    if skipped:
        lines += f"Skipped problems: {skipped}\n"

    itc = [r["itc"] for r in results.values() if r.get("itc")]
    if itc:
        first = sum(1 for i in itc if i.get("first_exit_pass"))
        converged = sum(1 for i in itc if i.get("converged"))
        lines += (f"Iterate-to-convergence: {first}/{len(itc)} passed at first exit, "
                  f"{converged}/{len(itc)} converged\n")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="Makefile target name that ran")
    parser.add_argument("--status", required=True, type=int, help="Exit status of the run")
    parser.add_argument("--duration", type=int, default=None, help="Duration in seconds")
    parser.add_argument("--prefix", default=None, help="Run output dir (BENCHMARK_PREFIX) to read results from")
    args = parser.parse_args()

    to_addr = os.environ.get("NOTIFY_EMAIL_TO")
    if not to_addr:
        print("notify_email: NOTIFY_EMAIL_TO not set, skipping email notification.", file=sys.stderr)
        return 0

    outcome = "SUCCESS" if args.status == 0 else f"FAILED (exit {args.status})"
    finished_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    duration_line = ""
    if args.duration is not None:
        minutes, seconds = divmod(args.duration, 60)
        duration_line = f"Duration: {minutes}m {seconds}s\n"

    summary = problem_summary(args.prefix) if args.prefix else ""

    body = (
        f"Benchmark target: {args.target}\n"
        f"Outcome: {outcome}\n"
        f"{summary}"
        f"{duration_line}"
        f"Finished at: {finished_at}\n"
    )
    subject = f"[cvdp_benchmark] {args.target} {outcome}"

    try:
        subprocess.run(
            ["mail", "-s", subject, to_addr],
            input=body.encode(),
            check=True,
            timeout=30,
        )
        print(f"notify_email: sent notification for '{args.target}' to {to_addr}.")
    except Exception as exc:
        print(f"notify_email: failed to send email ({exc}).", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
