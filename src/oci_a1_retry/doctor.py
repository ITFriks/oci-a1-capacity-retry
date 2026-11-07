"""`doctor`: read-only environment, OCI authentication and stack access checks."""

from __future__ import annotations

import os
import platform
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import oci

from .config import LoadedConfig, Settings, validate_settings
from .jobs import APPLY, StackInfo, format_time, short_ocid
from .oci_client import (
    OciCallError,
    OciSetupError,
    ResourceManagerGateway,
    describe_error,
    load_oci_config,
    oci_config_location,
)
from .runner import EXIT_OK, EXIT_STOPPED
from .shape import FREE_A1_MEMORY_GB, FREE_A1_OCPUS, SizeUnknown, a1_size, format_amount

GatewayFactory = Callable[[Settings], ResourceManagerGateway]


class _Report:
    def __init__(self) -> None:
        self.failures = 0

    def ok(self, text: str) -> None:
        print(f"[ OK ] {text}")

    def warn(self, text: str) -> None:
        print(f"[WARN] {text}")

    def fail(self, text: str) -> None:
        self.failures += 1
        print(f"[FAIL] {text}")

    def info(self, text: str) -> None:
        print(f"       {text}")


def run_doctor(
    loaded: LoadedConfig, *, gateway_factory: GatewayFactory, log_dir: Path, verbose: bool
) -> int:
    report = _Report()
    print("OCI A1 Capacity Retry - doctor")
    print("------------------------------")
    _check_python(report)
    _check_settings_file(loaded, report)
    settings = loaded.settings
    problems = validate_settings(settings)
    for problem in problems:
        report.fail(problem)
    if not problems:
        report.ok("Settings are valid")
    if _check_oci_profile(settings, report) and not problems:
        _check_api(settings, gateway_factory, report, verbose)
    elif problems:
        report.info("OCI API checks skipped until the problems above are fixed.")
    _check_log_dir(log_dir, report)
    print()
    if report.failures:
        print(f"{report.failures} problem(s) found.")
        return EXIT_STOPPED
    print("All checks passed. Next: 'oci-a1-retry status', then 'oci-a1-retry once'.")
    return EXIT_OK


def _check_python(report: _Report) -> None:
    # Older interpreters cannot even import this package (tomllib, 3.11+ syntax).
    report.ok(f"Python {platform.python_version()} ({sys.executable})")
    report.ok(f"OCI Python SDK {oci.__version__}")


def _check_settings_file(loaded: LoadedConfig, report: _Report) -> None:
    if loaded.path:
        report.ok(f"Config file: {loaded.path}")
        return
    searched = ", ".join(str(p) for p in loaded.searched)
    report.warn(f"No config file found (searched: {searched}); using environment/CLI values")


def _check_oci_profile(settings: Settings, report: _Report) -> bool:
    try:
        config = load_oci_config(settings)
    except OciSetupError as exc:
        report.fail(str(exc))
        return False
    report.ok(
        f"OCI config {oci_config_location(settings)}, profile [{settings.profile}], "
        f"region {config['region']}"
    )
    key_file = Path(os.path.expanduser(config.get("key_file", "")))
    if not key_file.is_file():
        report.fail(f"API key file not found: {key_file}")
        return False
    report.ok(f"API key file present: {key_file} (contents are never read by doctor)")
    mode = key_file.stat().st_mode
    if os.name == "posix" and mode & (stat.S_IRWXG | stat.S_IRWXO):
        report.warn(f"Key file is readable by other users; run: chmod 600 '{key_file}'")
    return True


def _check_api(
    settings: Settings, gateway_factory: GatewayFactory, report: _Report, verbose: bool
) -> None:
    try:
        gateway = gateway_factory(settings)
        stack = gateway.get_stack(settings.stack_ocid)
        report.ok(
            f"Authenticated; stack '{stack.display_name}' is {stack.lifecycle_state} "
            f"({short_ocid(stack.id, full=verbose)})"
        )
        if stack.lifecycle_state != "ACTIVE":
            report.fail("The stack must be ACTIVE before Apply jobs can run")
        jobs = gateway.list_jobs(settings.stack_ocid)
    except OciSetupError as exc:
        report.fail(str(exc))
        return
    except OciCallError as err:
        report.fail(describe_error(err, settings))
        return
    latest = next((job for job in jobs if job.operation == APPLY), None)
    detail = (
        f"latest Apply {latest.lifecycle_state} at {format_time(latest.time_created)}"
        if latest
        else "no Apply jobs yet"
    )
    report.ok(f"Can list Resource Manager jobs ({detail})")
    report.info("Permission to create jobs ('manage orm-jobs') is confirmed by the first 'once'.")
    _check_a1_size(gateway, stack, report)


def _check_a1_size(gateway: ResourceManagerGateway, stack: StackInfo, report: _Report) -> None:
    """Warn when the stack asks for more A1 than Always Free covers. Never a failure."""
    free = f"{FREE_A1_OCPUS} OCPUs and {FREE_A1_MEMORY_GB} GB"
    try:
        archive = gateway.get_stack_tf_config(stack.id)
        size = a1_size(archive, dict(stack.variables))
    except OciCallError as err:
        report.info(f"Could not download the stack's Terraform configuration ({err}).")
        report.info(f"Check the VM size yourself: Always Free covers {free} of A1 in total.")
        return
    except SizeUnknown as exc:
        report.info(f"Could not work out the A1 size from the stack: {exc}.")
        report.info(f"Check the VM size yourself: Always Free covers {free} of A1 in total.")
        return
    if size is None:
        report.warn(
            "The stack's Terraform has no VM.Standard.A1.Flex instance (modules are not read)."
        )
        return
    asks = (
        f"Stack asks for {format_amount(size.ocpus)} OCPUs and {format_amount(size.memory_gb)} GB "
        f"of A1 memory ({size.instances} instance{'s' if size.instances != 1 else ''})"
    )
    if size.within_free_tier:
        report.ok(f"{asks}: within Always Free ({free} in total)")
        report.info("Other A1 instances in your tenancy count toward the same allowance.")
        return
    report.warn(f"{asks}: more than Always Free covers ({free} in total since June 2026)")
    report.info("Free account: the Apply fails with a service limit error.")
    report.info(
        "Pay As You Go account: the VM is created and the part above the allowance is billed."
    )
    report.info("To stay free, lower ocpus and memory_in_gbs in the stack before 'once' or 'run'.")


def _check_log_dir(log_dir: Path, report: _Report) -> None:
    try:
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryFile(dir=log_dir):
            pass
    except OSError as exc:
        report.fail(f"Log directory {log_dir} is not writable: {exc}")
        return
    report.ok(f"Log directory writable: {log_dir}")
