"""The capacity retry loop.

Each cycle: list the stack's jobs -> attach to a running Apply job or create one
-> poll it to a terminal state -> success: stop; capacity failure: wait and
repeat; anything else: stop. There is exactly one retry mechanism (this module);
the SDK's own retries are disabled in oci_client.py.
"""

from __future__ import annotations

import logging
import socket
import time
import uuid
from collections.abc import Callable
from enum import Enum
from typing import Any, TypeVar

from .analysis import is_capacity_failure, useful_tail
from .config import Settings
from .jobs import (
    CANCELED,
    FAILED,
    SUCCEEDED,
    Action,
    Decision,
    JobInfo,
    StackInfo,
    decide,
    short_ocid,
)
from .logging_setup import LOGGER_NAME, PLAIN
from .oci_client import ErrorKind, OciCallError, ResourceManagerGateway
from .retry import Backoff

log = logging.getLogger(LOGGER_NAME)
T = TypeVar("T")

EXIT_OK = 0
EXIT_STOPPED = 1
EXIT_CONFIG = 2
EXIT_OCI_ERROR = 3
EXIT_NO_CAPACITY = 4
EXIT_GAVE_UP = 5
EXIT_INTERRUPTED = 130

# ponytail: a fixed count (~1 h at the 60 s cap) instead of a configurable window;
# a service manager (systemd Restart=on-failure) can restart the program afterwards.
MAX_CONSECUTIVE_ERRORS = 60
LOG_FETCH_ATTEMPTS = 3  # the log can lag a few seconds behind the FAILED state
RULE = "=" * 40


class Outcome(Enum):
    APPLIED = "applied"
    NOTHING_TO_DO = "nothing_to_do"
    NO_CAPACITY = "no_capacity"  # only in single-attempt mode
    APPLY_FAILED = "apply_failed"
    CANCELED = "canceled"
    BLOCKED = "blocked"

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self]


_EXIT_CODES = {
    Outcome.APPLIED: EXIT_OK,
    Outcome.NOTHING_TO_DO: EXIT_OK,
    Outcome.NO_CAPACITY: EXIT_NO_CAPACITY,
    Outcome.APPLY_FAILED: EXIT_STOPPED,
    Outcome.CANCELED: EXIT_STOPPED,
    Outcome.BLOCKED: EXIT_STOPPED,
}


class GaveUp(Exception):
    """Too many consecutive transient errors."""


class Runner:
    def __init__(
        self,
        gateway: ResourceManagerGateway,
        settings: Settings,
        *,
        sleep: Callable[[float], None] = time.sleep,
        on_success: Callable[[StackInfo, JobInfo], None] | None = None,
        verbose: bool = False,
    ) -> None:
        self._gateway = gateway
        self._settings = settings
        self._sleep = sleep
        self._on_success = on_success or (lambda stack, job: None)
        self._verbose = verbose
        self._throttle = self._new_backoff()
        self.current_job: JobInfo | None = None  # job being monitored (for Ctrl+C message)
        self.jobs_created = 0

    def load_stack(self) -> StackInfo:
        stack = self._read(self._gateway.get_stack, self._settings.stack_ocid)
        log.debug("Stack OCID %s (%s)", stack.id, stack.lifecycle_state)
        if stack.lifecycle_state != "ACTIVE":
            raise OciCallError(
                ErrorKind.PERMANENT,
                f"stack '{stack.display_name}' is {stack.lifecycle_state}, expected ACTIVE",
            )
        return stack

    def run(self, stack: StackInfo, *, loop: bool, force: bool) -> Outcome:
        """Run until a terminal outcome; ``loop=False`` makes at most one Apply attempt."""
        while True:
            job = self._obtain_job(force)
            if isinstance(job, Outcome):
                return job
            force = False  # --force bypasses the previous-success guard only once
            outcome = self._evaluate(self._monitor(job), stack)
            if outcome is not None:
                return outcome
            if not loop:
                return Outcome.NO_CAPACITY
            log.info("Next capacity attempt in %d seconds.", self._settings.capacity_retry_seconds)
            self._sleep(self._settings.capacity_retry_seconds)

    # -- discovery / creation ------------------------------------------------

    def _obtain_job(self, force: bool) -> JobInfo | Outcome:
        """Return a running Apply job, a newly created one, or a terminal outcome."""
        retry_token = uuid.uuid4().hex  # reused until this attempt's job exists
        errors = self._new_backoff()
        error_count = 0
        while True:
            jobs = self._read(self._gateway.list_jobs, self._settings.stack_ocid)
            decision = decide(jobs, force=force)
            if decision.action is not Action.CREATE:
                result = self._handle_existing(decision)
                if result is None:
                    continue
                return result
            try:
                return self._create(retry_token)
            except OciCallError as err:
                if err.kind is ErrorKind.THROTTLED:
                    self._wait_for_throttle(err)
                    continue
                if err.kind not in (ErrorKind.TRANSIENT, ErrorKind.CONFLICT):
                    raise
                error_count += 1
                if error_count >= MAX_CONSECUTIVE_ERRORS:
                    raise GaveUp(f"CreateJob failed {error_count} times in a row: {err}") from err
                delay = (
                    self._settings.job_poll_seconds
                    if err.kind is ErrorKind.CONFLICT
                    else errors.delay
                )
                errors = errors.advance()
                log.warning("CreateJob failed (%s). Re-checking jobs in %d seconds.", err, delay)
                self._sleep(delay)

    def _handle_existing(self, decision: Decision) -> JobInfo | Outcome | None:
        """Act on a non-CREATE decision; None means "look at the jobs again"."""
        action, job = decision.action, decision.job
        if job is None:  # decide() always attaches the job; fail closed rather than create
            raise RuntimeError(f"decision {action.value} carries no job")
        if action is Action.ATTACH:
            log.info(
                "Apply job %s is already %s; monitoring it instead of creating a new one.",
                self._id(job.id),
                job.lifecycle_state,
            )
            return job
        if action is Action.WAIT_FOR_OTHER:
            log.info(
                "Stack is busy with a %s job (%s, %s); waiting for it to finish.",
                job.operation,
                self._id(job.id),
                job.lifecycle_state,
            )
            self._monitor(job)
            return None
        if action is Action.NOTHING_TO_DO:
            log.info("The latest Apply job succeeded (%s).", self._id(job.id))
            log.info("Nothing to do. Use --force to submit another Apply anyway.")
            return Outcome.NOTHING_TO_DO
        self._banner(
            logging.ERROR,
            "STOPPED",
            f"The latest job on this stack is a DESTROY ({job.lifecycle_state}).",
            "Not re-creating resources automatically. Use --force to apply anyway.",
        )
        return Outcome.BLOCKED

    def _create(self, retry_token: str) -> JobInfo:
        log.info("Creating Resource Manager Apply job...")
        log.debug("CreateJob retry token %s", retry_token)
        job = self._gateway.create_apply_job(
            self._settings.stack_ocid,
            retry_token=retry_token,
            display_name=f"oci-a1-retry apply ({socket.gethostname()[:60]})",
        )
        self._throttle = self._throttle.reset()
        self.jobs_created += 1
        log.info("Job accepted: %s (attempt #%d)", self._id(job.id), self.jobs_created)
        log.debug("Job OCID %s", job.id)
        return job

    def _wait_for_throttle(self, err: OciCallError) -> None:
        delay = self._throttle.delay
        self._throttle = self._throttle.advance()
        log.warning("Resource Manager returned HTTP 429 TooManyRequests.")
        log.warning("Retrying CreateJob in %d seconds.", delay)
        log.debug("Throttle details: %s (opc-request-id %s)", err, err.request_id or "-")
        self._sleep(delay)

    # -- monitoring / evaluation ---------------------------------------------

    def _monitor(self, job: JobInfo) -> JobInfo:
        self.current_job = job
        state = job.lifecycle_state
        while job.is_active:
            self._sleep(self._settings.job_poll_seconds)
            job = self._read(self._gateway.get_job, job.id)
            self.current_job = job
            if job.lifecycle_state != state:
                state = job.lifecycle_state
                log.info("Job %s", state)
        self.current_job = None
        return job

    def _evaluate(self, job: JobInfo, stack: StackInfo) -> Outcome | None:
        """Terminal outcome for a finished job, or None for a capacity failure."""
        if job.lifecycle_state == SUCCEEDED:
            self._banner(
                logging.INFO,
                "SUCCESS",
                "OCI Resource Manager Apply succeeded.",
                "The VM should now be created.",
                "Retry loop stopped.",
            )
            log.info("Successful job: %s", self._id(job.id))
            self._on_success(stack, job)
            return Outcome.APPLIED
        if job.lifecycle_state == FAILED:
            log_text = self._job_log(job)
            if is_capacity_failure(log_text, job.failure_message):
                log.warning("No A1 host capacity available.")
                return None
            self._banner(
                logging.ERROR, "STOPPED", "Apply failed for a reason other than host capacity."
            )
            self._show_failure(job, log_text)
            return Outcome.APPLY_FAILED
        reason = " (canceled outside this program)" if job.lifecycle_state == CANCELED else ""
        self._banner(
            logging.ERROR, "STOPPED", f"Apply job ended in state {job.lifecycle_state}{reason}."
        )
        return Outcome.CANCELED if job.lifecycle_state == CANCELED else Outcome.APPLY_FAILED

    def _job_log(self, job: JobInfo) -> str:
        """The job's Terraform log; re-fetched briefly because it can lag behind FAILED."""
        for attempt in range(1, LOG_FETCH_ATTEMPTS + 1):
            try:
                text = self._read(self._gateway.get_job_log, job.id)
            except OciCallError as err:
                log.warning("Could not fetch the job log (%s).", err)
                text = ""
            if text.strip():
                return text
            if attempt < LOG_FETCH_ATTEMPTS:
                delay = self._settings.job_poll_seconds
                log.info("Job log not available yet; retrying in %d seconds.", delay)
                self._sleep(delay)
        log.warning("Job log unavailable; only the job's failure details can be checked.")
        return ""

    def _show_failure(self, job: JobInfo, log_text: str) -> None:
        if job.failure_code or job.failure_message:
            log.error("Failure details: %s %s", job.failure_code, job.failure_message)
        tail = useful_tail(log_text)
        if not tail:
            log.error("Job log unavailable; see OCI Console > Resource Manager > Jobs.")
            return
        log.error("Last %d log lines of job %s:", len(tail), self._id(job.id))
        for line in tail:
            log.error("  | %s", line, extra=PLAIN)

    # -- helpers -------------------------------------------------------------

    def _read(self, method: Callable[..., T], *args: Any) -> T:
        """Call a read-only API method, retrying 429 and transient errors with backoff."""
        backoff = self._new_backoff()
        failures = 0
        while True:
            try:
                return method(*args)
            except OciCallError as err:
                if err.kind is ErrorKind.TRANSIENT:
                    failures += 1
                    if failures >= MAX_CONSECUTIVE_ERRORS:
                        raise GaveUp(f"{failures} consecutive transient errors: {err}") from err
                elif err.kind is not ErrorKind.THROTTLED:
                    raise
                delay = backoff.delay
                backoff = backoff.advance()
                log.warning(
                    "OCI %s failed (%s). Retrying in %d seconds.",
                    err.operation or "call",
                    err,
                    delay,
                )
                self._sleep(delay)

    def _new_backoff(self) -> Backoff:
        return Backoff(self._settings.throttle_initial_seconds, self._settings.throttle_max_seconds)

    def _id(self, ocid: str) -> str:
        return short_ocid(ocid, full=self._verbose)

    @staticmethod
    def _banner(level: int, *lines: str) -> None:
        for line in (RULE, *lines, RULE):
            log.log(level, line, extra=PLAIN)
