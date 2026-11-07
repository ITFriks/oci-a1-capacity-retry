"""Optional success notifications. Failures here are logged and never affect the result."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

from .config import Settings
from .jobs import JobInfo, StackInfo
from .logging_setup import LOGGER_NAME

log = logging.getLogger(LOGGER_NAME)

TITLE = "OCI A1 capacity retry"
HOOK_TIMEOUT_SECONDS = 300
# Text is passed through environment variables, never interpolated into the script.
_WINDOWS_POPUP = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[System.Windows.Forms.MessageBox]::Show($env:OCI_A1_NOTIFY_MESSAGE, $env:OCI_A1_NOTIFY_TITLE)"
    " | Out-Null"
)


def notify_success(settings: Settings, stack: StackInfo, job: JobInfo) -> None:
    message = f"Apply succeeded for stack '{stack.display_name}'. The VM should now be created."
    if settings.notify_bell:
        _bell()
    if settings.notify_desktop:
        _desktop(message)
    if settings.success_command:
        _run_hook(settings.success_command, stack, job)


def _bell() -> None:
    # Only a real terminal beeps; in the journal or a log file "\a" is just junk.
    if sys.stdout is not None and sys.stdout.isatty():
        sys.stdout.write("\a")
        sys.stdout.flush()


def _desktop(message: str) -> None:
    try:
        if sys.platform == "win32":
            subprocess.Popen(  # detached: the dialog must not block or outlive-wait us
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_POPUP],
                env={**os.environ, "OCI_A1_NOTIFY_TITLE": TITLE, "OCI_A1_NOTIFY_MESSAGE": message},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", TITLE, message], check=False, timeout=10)
        else:
            log.debug("No desktop notification mechanism available.")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Desktop notification failed: %s", exc)


def _run_hook(command: str, stack: StackInfo, job: JobInfo) -> None:
    """Run the user's own success_command from their own config file."""
    env = {**os.environ, "OCI_A1_JOB_ID": job.id, "OCI_A1_STACK_NAME": stack.display_name}
    log.info("Running success_command...")
    try:
        result = subprocess.run(
            command, shell=True, env=env, check=False, timeout=HOOK_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("success_command failed to run: %s", exc)
        return
    if result.returncode != 0:
        log.warning("success_command exited with status %d.", result.returncode)
