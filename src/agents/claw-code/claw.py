from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.cvdp_prompt import build_cvdp_prompt


@dataclass(slots=True)
class ClawRunResult:
    """Structured result returned by the Claw Code adapter."""

    returncode: int
    stdout: str
    stderr: str
    prompt_path: str
    metadata: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ClawAgent:
    """
    Minimal adapter that lets CVDP call an external Claw Code CLI.

    The Docker entrypoint uses `agent.py` directly. This class mirrors the
    `opencode.py` adapter shape so local scripts/tests can invoke Claw without
    going through Docker.
    """

    def __init__(
        self,
        claw_executable: str = "claw",
        model: str = "sonnet",
        permission_mode: str = "danger-full-access",
        timeout_seconds: int = 600,
        skip_permissions: bool = False,
    ) -> None:
        self.claw_executable = claw_executable
        self.model = model
        self.permission_mode = permission_mode
        self.timeout_seconds = timeout_seconds
        self.skip_permissions = skip_permissions

    def build_prompt(
        self,
        task_prompt: str,
        workspace: str | os.PathLike[str],
        target_files: Optional[list[str]] = None,
        extra_instructions: Optional[str] = None,
    ) -> str:
        """Build the text prompt that will be passed to Claw."""
        workspace_path = Path(workspace).resolve()
        return build_cvdp_prompt(
            task_prompt,
            code_root=workspace_path,
            target_files=target_files,
            extra_instructions=extra_instructions,
        ) + "\n"

    def save_prompt(
        self,
        prompt_text: str,
        workspace: str | os.PathLike[str],
        filename: str = "cvdp_claw_prompt.txt",
    ) -> Path:
        """Persist the generated prompt inside the datapoint workspace."""
        workspace_path = Path(workspace).resolve()
        workspace_path.mkdir(parents=True, exist_ok=True)
        prompt_path = workspace_path / filename
        prompt_path.write_text(prompt_text, encoding="utf-8")
        return prompt_path

    def build_command(self, prompt_text: str) -> list[str]:
        """Build the CLI command used to invoke Claw Code."""
        command = [self.claw_executable]
        if self.model:
            command.extend(["--model", self.model])
        if self.skip_permissions:
            command.append("--dangerously-skip-permissions")
        elif self.permission_mode:
            command.extend(["--permission-mode", self.permission_mode])
        command.extend(["--output-format", "text", "prompt", prompt_text])
        return command

    def run(
        self,
        task_prompt: str,
        workspace: str | os.PathLike[str],
        target_files: Optional[list[str]] = None,
        extra_instructions: Optional[str] = None,
    ) -> ClawRunResult:
        """
        End-to-end helper used by CVDP.

        Current flow:
        - build prompt
        - save prompt to workspace
        - invoke Claw in one-shot prompt mode
        - try to parse the final stdout line as JSON metadata
        """
        prompt_text = self.build_prompt(
            task_prompt=task_prompt,
            workspace=workspace,
            target_files=target_files,
            extra_instructions=extra_instructions,
        )
        prompt_path = self.save_prompt(prompt_text, workspace)

        completed = subprocess.run(
            self.build_command(prompt_text),
            cwd=Path(workspace).resolve(),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )

        metadata = self._extract_json_metadata(completed.stdout)

        return ClawRunResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            prompt_path=str(prompt_path),
            metadata=metadata,
        )

    @staticmethod
    def _extract_json_metadata(stdout: str) -> dict[str, Any] | None:
        """
        Best-effort parser for a JSON object printed on the last stdout line.
        """
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        for line in reversed(lines):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None


def run_claw_once(
    task_prompt: str,
    workspace: str | os.PathLike[str],
    claw_executable: str = "claw",
    model: str = "sonnet",
    permission_mode: str = "danger-full-access",
    timeout_seconds: int = 600,
    skip_permissions: bool = False,
    target_files: Optional[list[str]] = None,
    extra_instructions: Optional[str] = None,
) -> ClawRunResult:
    """Convenience wrapper for simple use without manually instantiating the class."""
    adapter = ClawAgent(
        claw_executable=claw_executable,
        model=model,
        permission_mode=permission_mode,
        timeout_seconds=timeout_seconds,
        skip_permissions=skip_permissions,
    )
    return adapter.run(
        task_prompt=task_prompt,
        workspace=workspace,
        target_files=target_files,
        extra_instructions=extra_instructions,
    )
