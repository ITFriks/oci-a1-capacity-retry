"""Per-user default locations, without third-party dependencies.

Windows: config in %APPDATA%, logs in %LOCALAPPDATA%.
Linux and others: XDG base directories (~/.config and ~/.local/state).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

APP_NAME = "oci-a1-capacity-retry"


def _is_windows(platform: str) -> bool:
    return platform.startswith("win")


def user_config_dir(platform: str = sys.platform, env: Mapping[str, str] = os.environ) -> Path:
    if _is_windows(platform):
        base = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def user_state_dir(platform: str = sys.platform, env: Mapping[str, str] = os.environ) -> Path:
    if _is_windows(platform):
        base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / APP_NAME
