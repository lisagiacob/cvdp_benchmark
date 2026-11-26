"""
Single source of truth for the CVDP benchmark prompt template.

Every external-CLI agent adapter (Claude Code, OpenCode, Claw Code, ...)
wraps the raw task prompt from prompt.json in the same "Goal / constraints /
context" instructions before handing it to its CLI. Edit the template here
and every adapter picks it up.
"""

from __future__ import annotations

from pathlib import Path

# Iterate-to-convergence only: what each task category actually asks for, so the
# agent is not told to "focus on the RTL" when the deliverable is a testbench or
# assertions. Categories not listed are RTL design/modification/debug tasks.
CATEGORY_GUIDANCE = {
    "cid012": (
        "Task type: testbench stimulus generation. Write the SystemVerilog testbench in verif/ and do "
        "not change the design in rtl/. The harness simulates your testbench with coverage enabled "
        "and requires the coverage target to be reached."
    ),
    "cid013": (
        "Task type: testbench checker generation. Write the testbench/checker in verif/. The harness "
        "runs it on the correct design, where the simulation must exit with status 0, and on "
        "bug-injected variants, where it must exit with a non-zero status. Report every mismatch "
        "with $error or $fatal: a $display alone does not change the exit status and is not detected."
    ),
    "cid014": (
        "Task type: assertion generation. Add SystemVerilog assertions to the RTL. They must never "
        "fire on the correct design under the harness stimulus, including at time 0, during and right "
        "after reset, and while inputs are X/Z: use `disable iff` on reset, guard with $isunknown, and "
        "take care with $past/$changed/$rose/$stable on the first sampled cycle. Use concurrent "
        "`assert property` checks, not procedural if/$error: the harness measures assertion coverage "
        "and requires 100%."
    ),
}
DEFAULT_CATEGORY_GUIDANCE = (
    "Task type: RTL design, modification or debug. Edit the design in rtl/ so that it meets the "
    "specification."
)


def build_cvdp_prompt(
    prompt_text: str,
    payload: dict | None = None,
    *,
    code_root: str | Path = "/code",
    target_files: list[str] | None = None,
    extra_instructions: str | None = None,
    iterate_to_convergence: bool = False,
    category: str | None = None,
    harness_visible: bool = False,
    harness_root: str | None = None,
    heavy: bool = False,
) -> str:
    """Build the benchmark-aware prompt passed to an external coding CLI.

    `target_files` takes precedence over `payload["files"]` when both are given.
    With `iterate_to_convergence` the prompt explains that the official harness
    grades the work after the session and that its output comes back until it
    passes; `category` selects the task-type guidance and `harness_visible`
    says whether src/ (the harness) is mounted. Context-heavy datapoints
    (`heavy`) are a full project checkout instead of the rtl/verif/docs layout,
    and their harness is mounted under `harness_root` (outside the repo).
    """
    code_root = str(code_root)
    payload = payload or {}

    lines: list[str] = [
        "You are solving a CVDP RTL benchmark task.",
        "",
        "Goal:",
        prompt_text.strip() if isinstance(prompt_text, str) else prompt_text,
        "",
        f"Repository root: {code_root}",
        "",
    ]

    if iterate_to_convergence:
        lines += [
            CATEGORY_GUIDANCE.get(category or "", DEFAULT_CATEGORY_GUIDANCE),
            "",
            "Important constraints:",
            "- Modify the repository files directly using your file tools.",
            "- Do not only describe the solution — write the actual code.",
            "- Read relevant files before editing.",
            "- Follow the specification in the task and in docs/: do not special-case the test "
            "vectors, hard-code expected values or detect the simulation environment.",
            "",
            "Directory layout:",
        ]
        if heavy:
            lines += [
                f"- {code_root}        — the project repository (a real codebase: explore it to find "
                "the files the task refers to)",
                f"- {code_root}/rundir — transient run artifacts (ignore unless needed)",
            ]
        else:
            lines += [
                f"- {code_root}/rtl    — RTL design files",
                f"- {code_root}/verif  — verification files (testbenches)",
                f"- {code_root}/docs   — documentation (read for context)",
                f"- {code_root}/rundir — transient run artifacts (ignore unless needed)",
            ]
        if harness_visible:
            h_root = str(harness_root or code_root)
            lines += [
                f"- {h_root}/src    — the official benchmark harness (READ-ONLY): the tests your "
                "work is graded with",
                f"- {h_root}/docker-compose.yml — how the official harness is run (READ-ONLY)",
                "",
                f"You may run the harness from {h_root}/src yourself if the tools in your environment "
                "allow it, but the official run may use a different environment, so treat local "
                "results as indicative.",
            ]
    else:
        lines += [
            "Important constraints:",
            "- Modify the repository files directly using your file tools.",
            "- Focus on the RTL implementation needed for the harness to pass.",
            "- Do not only describe the solution — write the actual code.",
            "- Read relevant files before editing.",
            "- Prefer targeted edits to the most likely design file(s).",
            "",
            "Directory layout:",
            f"- {code_root}/rtl    — RTL design files (edit these)",
            f"- {code_root}/verif  — verification collateral (read for context)",
            f"- {code_root}/docs   — documentation (read for context)",
            f"- {code_root}/rundir — transient run artifacts (ignore unless needed)",
        ]

    if target_files is None:
        raw_files = payload.get("files")
        if isinstance(raw_files, list):
            target_files = raw_files

    if target_files:
        clean_files = [f.strip() for f in target_files if isinstance(f, str) and f.strip()]
        if clean_files:
            lines += ["", "Primary target files:"] + [f"- {f}" for f in clean_files]

    if extra_instructions:
        lines += ["", "Additional instructions:", extra_instructions.strip()]

    if iterate_to_convergence:
        lines += [
            "",
            "How this task is graded:",
            "When you stop, the official benchmark harness is run on the repository. If it fails, "
            "you will receive its output and must continue fixing the problem. The task is complete "
            "only when the official harness passes, so verify your work before stopping.",
        ]
    else:
        lines += [
            "",
            "Expected outcome:",
            "Leave the repository in a state where the benchmark harness can validate it.",
        ]

    return "\n".join(lines)
