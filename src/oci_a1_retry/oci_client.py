"""Narrow wrapper around the OCI Resource Manager SDK client.

Only read operations and "create Apply job" are exposed: nothing here can
destroy, cancel, delete or modify a stack. The SDK's automatic retries and its
circuit breaker are disabled so that runner.py owns the one and only retry
strategy and timing stays predictable.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import Enum
from typing import Any

import oci
from oci.resource_manager import models

from .analysis import strip_control
from .config import Settings
from .jobs import JobInfo, StackInfo

CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 60
LIST_JOBS_LIMIT = 50


class ErrorKind(Enum):
    THROTTLED = "throttled"  # HTTP 429
    TRANSIENT = "transient"  # HTTP 5xx, timeouts, connection problems
    CONFLICT = "conflict"  # HTTP 409, e.g. the stack is locked by another job
    AUTH = "auth"  # HTTP 401
    NOT_FOUND = "not_found"  # HTTP 404, which OCI also returns for "not authorized"
    PERMANENT = "permanent"  # any other 4xx


class OciSetupError(Exception):
    """OCI config file/profile/key problem found before any API call."""


class OciCallError(Exception):
    """A classified failure of one OCI API call."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        status: int | None = None,
        code: str = "",
        operation: str = "",
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code
        self.operation = operation
        self.request_id = request_id


# OSError covers connection resets, timeouts, DNS and TLS failures (the SDK's own
# RequestException/ConnectTimeout derive from it too).
_NETWORK_ERRORS = (oci.exceptions.RequestException, oci.exceptions.ConnectTimeout, OSError)


def classify(exc: BaseException) -> OciCallError | None:
    """Map an SDK exception to an OciCallError; None means "not an OCI call failure"."""
    if isinstance(exc, oci.exceptions.ServiceError):
        return OciCallError(
            _kind_for_status(exc.status),
            strip_control(f"HTTP {exc.status} {exc.code}: {exc.message}"),
            status=exc.status,
            code=exc.code or "",
            operation=getattr(exc, "operation_name", None) or "",
            request_id=getattr(exc, "request_id", None) or "",
        )
    if isinstance(exc, _NETWORK_ERRORS):
        first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        return OciCallError(
            ErrorKind.TRANSIENT, f"network error: {strip_control(first_line)[:200]}"
        )
    return None


def _kind_for_status(status: int) -> ErrorKind:
    if status == 429:
        return ErrorKind.THROTTLED
    if status == 409:
        return ErrorKind.CONFLICT
    if status == 401:
        return ErrorKind.AUTH
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status >= 500 or status == 408:
        return ErrorKind.TRANSIENT
    return ErrorKind.PERMANENT


def describe_error(err: OciCallError, settings: Settings, region: str = "") -> str:
    """User-facing explanation with the most likely fix."""
    region = region or settings.region or "the profile's region"
    if err.kind is ErrorKind.AUTH:
        return (
            f"OCI rejected the API credentials ({err}). Check the user and tenancy OCIDs, "
            "the fingerprint, that the matching public key is uploaded "
            "(OCI Console > Profile > API keys), and that this computer's clock is correct."
        )
    if err.kind is ErrorKind.NOT_FOUND:
        return (
            f"Stack or job not found, or not authorized ({err}). Check stack_ocid, that the "
            f"stack lives in region {region}, and that IAM policies allow your user "
            "'read orm-stacks' and 'manage orm-jobs' in the stack's compartment."
        )
    if err.kind is ErrorKind.PERMANENT:
        return f"OCI rejected the request ({err}). This is not retried."
    return f"OCI API call failed ({err})."


class ResourceManagerGateway:
    """The only OCI operations this application performs."""

    def __init__(self, client: Any, region: str = "") -> None:
        self._client = client
        self.region = region

    def get_stack(self, stack_id: str) -> StackInfo:
        stack = _call(self._client.get_stack, stack_id)
        variables = getattr(stack, "variables", None)
        return StackInfo(
            id=stack.id,
            display_name=strip_control(stack.display_name or ""),
            lifecycle_state=stack.lifecycle_state or "",
            compartment_id=stack.compartment_id or "",
            variables=tuple(sorted((str(k), str(v)) for k, v in variables.items()))
            if isinstance(variables, dict)
            else (),
        )

    def get_stack_tf_config(self, stack_id: str) -> bytes:
        """The stack's Terraform configuration as a zip archive (read-only; doctor only)."""
        data = _call(self._client.get_stack_tf_config, stack_id)
        # The SDK returns the raw HTTP response for this "stream" operation.
        return data if isinstance(data, (bytes, bytearray)) else data.content

    def list_jobs(self, stack_id: str) -> list[JobInfo]:
        """Most recent jobs of the stack, newest first."""
        jobs = _call(
            self._client.list_jobs,
            stack_id=stack_id,
            sort_by="TIMECREATED",
            sort_order="DESC",
            limit=LIST_JOBS_LIMIT,
        )
        return [_job_info(job) for job in jobs]

    def create_apply_job(self, stack_id: str, *, retry_token: str, display_name: str) -> JobInfo:
        """Submit an auto-approved Apply job for the existing stack (nothing else)."""
        details = models.CreateJobDetails(
            stack_id=stack_id,
            display_name=display_name,
            job_operation_details=models.CreateApplyJobOperationDetails(
                execution_plan_strategy="AUTO_APPROVED"
            ),
        )
        # The retry token makes a re-sent request (after a timeout) return the
        # job that was already created instead of creating a second one.
        return _job_info(_call(self._client.create_job, details, opc_retry_token=retry_token))

    def get_job(self, job_id: str) -> JobInfo:
        return _job_info(_call(self._client.get_job, job_id))

    def get_job_log(self, job_id: str) -> str:
        content = _call(self._client.get_job_logs_content, job_id)
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return content or ""


def _call(method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return method(*args, **kwargs).data
    except Exception as exc:
        err = classify(exc)
        if err is None:
            raise
        raise err from exc


def _job_info(job: Any) -> JobInfo:
    failure = getattr(job, "failure_details", None)
    return JobInfo(
        id=job.id,
        operation=job.operation or "",
        lifecycle_state=job.lifecycle_state or "",
        time_created=job.time_created,
        time_finished=job.time_finished,
        display_name=strip_control(job.display_name or ""),
        failure_code=strip_control(failure.code or "") if failure else "",
        failure_message=strip_control(failure.message or "") if failure else "",
    )


def oci_config_location(settings: Settings) -> str:
    """The OCI config file the SDK will read (same lookup order as oci.config.from_file)."""
    if settings.oci_config_file:
        return os.path.expanduser(settings.oci_config_file)
    default = os.path.expanduser(oci.config.DEFAULT_LOCATION)
    if os.path.isfile(default) or not os.environ.get("OCI_CONFIG_FILE"):
        return default
    return os.path.expanduser(os.environ["OCI_CONFIG_FILE"])


def load_oci_config(settings: Settings) -> dict[str, Any]:
    """Read and validate the OCI profile; ``settings.region`` overrides its region."""
    location = settings.oci_config_file or oci.config.DEFAULT_LOCATION
    try:
        config = oci.config.from_file(file_location=location, profile_name=settings.profile)
    except oci.exceptions.ConfigFileNotFound as exc:
        raise OciSetupError(
            f"OCI config file not found ({oci_config_location(settings)}). Create it with "
            "'oci setup config' or from OCI Console > Profile > API keys > Add API key; "
            "see README section 'OCI authentication'."
        ) from exc
    except oci.exceptions.ProfileNotFound as exc:
        raise OciSetupError(
            f"Profile [{settings.profile}] not found in {oci_config_location(settings)}. "
            "Use --profile or the 'profile' setting."
        ) from exc
    except (oci.exceptions.ClientError, ValueError) as exc:
        raise OciSetupError(f"OCI profile [{settings.profile}] is invalid: {exc}") from exc
    if settings.region:
        config = {**config, "region": settings.region}
    try:
        oci.config.validate_config(config)
    except oci.exceptions.InvalidConfig as exc:
        raise OciSetupError(
            f"OCI profile [{settings.profile}] is incomplete or malformed: {exc}"
        ) from exc
    return config


def build_gateway(settings: Settings) -> ResourceManagerGateway:
    config = load_oci_config(settings)
    try:
        client = oci.resource_manager.ResourceManagerClient(
            config,
            retry_strategy=oci.retry.NoneRetryStrategy(),
            circuit_breaker_strategy=oci.circuit_breaker.NoCircuitBreakerStrategy(),
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
        )
    except (oci.exceptions.ClientError, OSError, ValueError) as exc:
        raise OciSetupError(
            f"Cannot load the API signing key for profile [{settings.profile}]: {exc} "
            "Check 'key_file' (and 'pass_phrase' if the key is encrypted) in the OCI config."
        ) from exc
    return ResourceManagerGateway(client, region=config["region"])
