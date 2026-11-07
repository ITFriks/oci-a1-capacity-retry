"""Command-line entry point: oci-a1-retry {doctor,status,run,once}."""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .config import ConfigError, Settings, load_settings, validate_settings
from .doctor import GatewayFactory, run_doctor
from .jobs import StackInfo, short_ocid
from .logging_setup import LOGGER_NAME, PLAIN, setup_logging
from .notify import notify_success
from .oci_client import (
    OciCallError,
    OciSetupError,
    ResourceManagerGateway,
    build_gateway,
    describe_error,
)
from .paths import user_state_dir
from .runner import (
    EXIT_CONFIG,
    EXIT_GAVE_UP,
    EXIT_INTERRUPTED,
    EXIT_OCI_ERROR,
    EXIT_STOPPED,
    GaveUp,
    Runner,
)
from .status import run_status

log = logging.getLogger(LOGGER_NAME)

DESCRIPTION = (
    "Re-submit an existing OCI Resource Manager Apply job until it succeeds "
    "(for 'Out of host capacity' errors on VM.Standard.A1.Flex)."
)
EPILOG = """\
commands:
  doctor   check Python, OCI config/profile/region and stack access (read-only)
  status   show the stack, recent jobs and what 'run' would do (read-only)
  once     at most one Apply attempt, then report the result
  run      retry Apply until it succeeds (stops on success or on a non-capacity error)

Start with 'doctor', then 'status', then 'once', and only then 'run'.
"""


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", help="config.toml to use")
    common.add_argument("--stack-id", "--stack-ocid", dest="stack_ocid", metavar="OCID")
    common.add_argument("--region", help="OCI region, e.g. eu-frankfurt-1 (default: from profile)")
    common.add_argument("--profile", help="profile in the OCI config file (default: DEFAULT)")
    common.add_argument("--oci-config-file", metavar="PATH", help="default: ~/.oci/config")
    common.add_argument("-v", "--verbose", action="store_true", help="debug output, full OCIDs")

    parser = argparse.ArgumentParser(
        prog="oci-a1-retry",
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("doctor", parents=[common], help="check environment and OCI access")
    status = sub.add_parser("status", parents=[common], help="show stack and job status")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    for name, text in (("run", "retry Apply until it succeeds"), ("once", "one Apply attempt")):
        command = sub.add_parser(name, parents=[common], help=text)
        command.add_argument(
            "--force",
            action="store_true",
            help="submit an Apply even if the latest Apply already succeeded",
        )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    gateway_factory: GatewayFactory = build_gateway,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    _make_console_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_CONFIG
    try:
        loaded = load_settings(_cli_values(args), config_path=args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    settings = loaded.settings
    log_dir = Path(settings.log_dir).expanduser() if settings.log_dir else user_state_dir()
    try:
        if args.command == "doctor":
            return run_doctor(
                loaded, gateway_factory=gateway_factory, log_dir=log_dir, verbose=args.verbose
            )
        problems = validate_settings(settings)
        if problems:
            print("Configuration problems:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return EXIT_CONFIG
        return _run_command(args, settings, log_dir, gateway_factory, sleep)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


def _run_command(
    args: argparse.Namespace,
    settings: Settings,
    log_dir: Path,
    gateway_factory: GatewayFactory,
    sleep: Callable[[float], None],
) -> int:
    as_json = getattr(args, "json", False)
    log_file = setup_logging(
        verbose=args.verbose,
        log_dir=log_dir,
        stream=sys.stderr if as_json else sys.stdout,
        console_level=logging.WARNING if as_json else None,
    )
    runner: Runner | None = None
    gateway: ResourceManagerGateway | None = None
    try:
        gateway = gateway_factory(settings)
        if args.command == "status":
            return run_status(gateway, settings, as_json=as_json, verbose=args.verbose)
        runner = Runner(
            gateway,
            settings,
            sleep=sleep,
            verbose=args.verbose,
            on_success=lambda stack, job: notify_success(settings, stack, job),
        )
        stack = runner.load_stack()
        loop = args.command == "run"
        _print_header(settings, gateway.region, stack, log_file, loop=loop, verbose=args.verbose)
        return runner.run(stack, loop=loop, force=args.force).exit_code
    except KeyboardInterrupt:
        _report_interrupt(runner, args.verbose)
        return EXIT_INTERRUPTED
    except OciSetupError as exc:
        log.error("OCI authentication is not set up correctly: %s", exc)
        log.error("Run 'oci-a1-retry doctor' for a step-by-step check.")
        return EXIT_OCI_ERROR
    except OciCallError as err:
        log.error(describe_error(err, settings, gateway.region if gateway else ""))
        log.debug("opc-request-id: %s", err.request_id or "-")
        return EXIT_OCI_ERROR
    except GaveUp as exc:
        log.error("Giving up: %s", exc)
        return EXIT_GAVE_UP
    except Exception as exc:  # last resort: a clear message instead of a raw traceback
        log.error("Unexpected error: %s: %s", type(exc).__name__, exc)
        log.error("Details are in the log file; run with -v to see them here.")
        log.debug("Traceback:", exc_info=True)
        return EXIT_STOPPED


def _report_interrupt(runner: Runner | None, verbose: bool) -> None:
    log.info("Retry loop stopped by user.")
    job = runner.current_job if runner else None
    if job is not None:
        log.info(
            "Job %s is still %s in OCI. It was NOT canceled and will finish on its own.",
            short_ocid(job.id, full=verbose),
            job.lifecycle_state,
        )
    else:
        log.info("Nothing was canceled in OCI; the next start re-checks the stack's jobs.")


def _print_header(
    settings: Settings,
    region: str,
    stack: StackInfo,
    log_file: Path | None,
    *,
    loop: bool,
    verbose: bool,
) -> None:
    title = "OCI A1 Capacity Retry"
    rows: tuple[tuple[str, str], ...] = (
        ("Region", region),
        ("Stack", stack.display_name),
        ("Stack OCID", short_ocid(stack.id, full=verbose)),
        ("Profile", settings.profile),
        ("Retry delay", f"{settings.capacity_retry_seconds} seconds"),
        ("Mode", "run (retry until Apply succeeds)" if loop else "once (single Apply attempt)"),
        ("Log file", str(log_file) if log_file else "(console only)"),
    )
    if settings.success_command:
        rows = (*rows, ("Success cmd", settings.success_command))
    log.info(title, extra=PLAIN)
    log.info("-" * len(title), extra=PLAIN)
    for label, value in rows:
        log.info("%-13s %s", f"{label}:", value, extra=PLAIN)
    log.info("", extra=PLAIN)


def _cli_values(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "stack_ocid": args.stack_ocid,
        "region": args.region,
        "profile": args.profile,
        "oci_config_file": args.oci_config_file,
    }


def _make_console_safe() -> None:
    # ponytail: a non-UTF-8 console (e.g. redirected output on Windows) prints '?'
    # for odd characters instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(errors="replace")
