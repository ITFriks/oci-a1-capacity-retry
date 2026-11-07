"""Classify a failed Apply job from its Terraform log; clean remote text for display."""

from __future__ import annotations

import re

# Matches e.g. "Error: 500-InternalError, Out of host capacity." and "Out of capacity".
CAPACITY_PATTERN = re.compile(r"out of (?:host )?capacity", re.IGNORECASE)
TAIL_LINES = 30
# Terminal escape sequences (OSC, CSI, other ESC) and C0/C1 control characters except
# tab. Text from OCI is echoed to a terminal, so it must not be able to drive it.
_CONTROL = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-_]"
    r"|[\x00-\x08\x0a-\x1f\x7f-\x9f]"
)


def strip_control(text: str) -> str:
    return _CONTROL.sub("", text)


def is_capacity_failure(*texts: str | None) -> bool:
    return any(text and CAPACITY_PATTERN.search(text) for text in texts)


def useful_tail(log_text: str, limit: int = TAIL_LINES) -> list[str]:
    """The last ``limit`` non-blank log lines, stripped of control characters."""
    lines = (strip_control(line).rstrip() for line in log_text.splitlines())
    return [line for line in lines if line.strip()][-limit:]
