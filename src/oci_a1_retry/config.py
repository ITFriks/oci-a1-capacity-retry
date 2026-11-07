"""Application settings.

Precedence (highest first): command-line arguments, environment variables,
config.toml, built-in defaults. OCI credentials are never stored here; they
stay in the standard OCI config file (~/.oci/config).
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .paths import user_config_dir

CONFIG_FILENAME = "config.toml"
CONFIG_PATH_ENV = "OCI_A1_CONFIG"
ENV_PREFIX = "OCI_A1_"

STACK_OCID_RE = re.compile(r"ocid1\.ormstack\.[a-z0-9-]+\.[a-z0-9.-]*[a-z0-9]")
REGION_RE = re.compile(r"[a-z]+(?:-[a-z]+)+-[0-9]+")

# (setting, minimum, maximum) - the minimums keep the tool from hammering the API.
_RANGES = (
    ("capacity_retry_seconds", 60, 86_400),
    ("job_poll_seconds", 5, 600),
    ("throttle_initial_seconds", 1, 600),
    ("throttle_max_seconds", 1, 3_600),
)
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_TYPE_NAMES = {str: "a string", int: "an integer", bool: "true or false"}


class ConfigError(Exception):
    """The configuration could not be read or has the wrong shape."""


@dataclass(frozen=True)
class Settings:
    stack_ocid: str = ""
    region: str = ""  # empty: use the region of the OCI profile
    profile: str = "DEFAULT"
    oci_config_file: str = ""  # empty: ~/.oci/config ($OCI_CONFIG_FILE if that is missing)
    capacity_retry_seconds: int = 300
    job_poll_seconds: int = 15
    throttle_initial_seconds: int = 5
    throttle_max_seconds: int = 60
    log_dir: str = ""  # empty: per-user state directory
    notify_bell: bool = True
    notify_desktop: bool = True
    success_command: str = ""


SETTING_TYPES: dict[str, type] = {f.name: type(f.default) for f in fields(Settings)}


@dataclass(frozen=True)
class LoadedConfig:
    settings: Settings
    path: Path | None  # the config file that was read, if any
    searched: tuple[Path, ...]  # where a config file was looked for


def default_config_paths(env: Mapping[str, str], cwd: Path) -> tuple[Path, ...]:
    return (cwd / CONFIG_FILENAME, user_config_dir(env=env) / CONFIG_FILENAME)


def load_settings(
    cli: Mapping[str, Any] | None = None,
    *,
    config_path: str | None = None,
    env: Mapping[str, str] = os.environ,
    cwd: Path | None = None,
) -> LoadedConfig:
    """Merge defaults, config file, environment and CLI values (later wins)."""
    path, searched = _find_config_file(config_path, env, cwd or Path.cwd())
    values: dict[str, Any] = _read_toml(path) if path else {}
    values |= _from_env(env)
    values |= {
        key: _coerce(key, value, "command line")
        for key, value in (cli or {}).items()
        if value is not None
    }
    return LoadedConfig(Settings(**values), path, searched)


def validate_settings(settings: Settings) -> list[str]:
    """Return human-readable problems; an empty list means the settings are usable."""
    problems = []
    if not settings.stack_ocid:
        problems.append(
            "stack_ocid is not set. Put it in config.toml, set OCI_A1_STACK_OCID, "
            "or pass --stack-id."
        )
    elif not STACK_OCID_RE.fullmatch(settings.stack_ocid):
        problems.append(_stack_ocid_problem(settings.stack_ocid))
    if settings.region and not REGION_RE.fullmatch(settings.region):
        problems.append(
            f"region {settings.region!r} is not a valid OCI region identifier "
            "(e.g. eu-frankfurt-1)."
        )
    if not settings.profile:
        problems.append("profile must not be empty (the default is DEFAULT).")
    for key, low, high in _RANGES:
        value = getattr(settings, key)
        if not low <= value <= high:
            problems.append(f"{key} must be between {low} and {high}, got {value}.")
    if settings.throttle_max_seconds < settings.throttle_initial_seconds:
        problems.append("throttle_max_seconds must be >= throttle_initial_seconds.")
    return problems


def _stack_ocid_problem(ocid: str) -> str:
    if "REPLACE" in ocid:
        return "stack_ocid still contains the example placeholder; set your real stack OCID."
    parts = ocid.split(".")
    if len(parts) > 2 and parts[0] == "ocid1" and parts[1] != "ormstack":
        return (
            f"stack_ocid is an '{parts[1]}' OCID, but a Resource Manager stack OCID "
            "(ocid1.ormstack...) is required."
        )
    return (
        "stack_ocid is not a valid Resource Manager stack OCID (expected "
        "ocid1.ormstack.oc1.<region>.<id>, copy it from OCI Console > "
        "Developer Services > Resource Manager > Stacks)."
    )


def _find_config_file(
    explicit: str | None, env: Mapping[str, str], cwd: Path
) -> tuple[Path | None, tuple[Path, ...]]:
    chosen = explicit or env.get(CONFIG_PATH_ENV)
    if chosen:
        path = Path(chosen).expanduser()
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path, (path,)
    searched = default_config_paths(env, cwd)
    return next((p for p in searched if p.is_file()), None), searched


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig")  # tolerate the BOM Notepad adds
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        hint = ""
        if "\\" in text:
            hint = (
                " Hint: inside double quotes a backslash starts an escape sequence; "
                "write Windows paths in single quotes, e.g. log_dir = 'C:\\Users\\me\\logs'."
            )
        raise ConfigError(f"{path} is not valid TOML: {exc}.{hint}") from exc
    unknown = sorted(set(data) - SETTING_TYPES.keys())
    if unknown:
        raise ConfigError(
            f"{path}: unknown setting(s): {', '.join(unknown)}. "
            f"Valid settings: {', '.join(SETTING_TYPES)}."
        )
    return {key: _coerce(key, value, str(path)) for key, value in data.items()}


def _from_env(env: Mapping[str, str]) -> dict[str, Any]:
    values = {}
    for key in SETTING_TYPES:
        name = ENV_PREFIX + key.upper()
        raw = env.get(name)
        if raw:
            values[key] = _coerce(key, raw, f"environment variable {name}")
    return values


def _coerce(key: str, value: Any, source: str) -> Any:
    expected = SETTING_TYPES[key]
    if isinstance(value, str) and expected is not str:
        value = _parse_text(value.strip(), expected)
    # type() rather than isinstance(): a TOML boolean must not pass as an integer.
    if type(value) is not expected:
        raise ConfigError(f"{source}: '{key}' must be {_TYPE_NAMES[expected]}, got {value!r}.")
    return value.strip() if isinstance(value, str) else value


def _parse_text(text: str, expected: type) -> Any:
    if expected is int:
        try:
            return int(text)
        except ValueError:
            return text
    lowered = text.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return text
