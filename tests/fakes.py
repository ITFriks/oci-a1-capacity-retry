"""Test doubles and fixtures. Nothing here talks to OCI: the fake client stands in for
oci.resource_manager.ResourceManagerClient and raises real SDK exception types."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import oci
import pytest
from oci.resource_manager import models

from oci_a1_retry.config import Settings
from oci_a1_retry.oci_client import ResourceManagerGateway

STACK_OCID = "ocid1.ormstack.oc1.eu-frankfurt-1.amaaaaaatestfakestack000000000000001"
STACK_NAME = "my-a1-stack"
CAPACITY_LOG = """\
Terraform v1.5.7
oci_core_instance.vm: Creating...
Error: 500-InternalError, Out of host capacity.
Suggestion: The service for this resource encountered an error. Please contact support
  with oci_core_instance.vm,
  on main.tf line 12, in resource "oci_core_instance" "vm":
"""
OTHER_FAILURE_LOG = """\
oci_core_instance.vm: Creating...
Error: 400-InvalidParameter, Invalid shape config: ocpus must be at least 1
  with oci_core_instance.vm,
  on main.tf line 12, in resource "oci_core_instance" "vm":
"""
_BASE_TIME = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Attempt:
    """What happens to a job created by the fake client."""

    final_state: str = "SUCCEEDED"
    log: str = ""


def service_error(
    status: int, code: str, message: str, operation: str = "create_job"
) -> oci.exceptions.ServiceError:
    return oci.exceptions.ServiceError(
        status=status,
        code=code,
        headers={"opc-request-id": "test-request-id"},
        message=message,
        operation_name=operation,
        target_service="resource_manager",
    )


def throttled() -> oci.exceptions.ServiceError:
    return service_error(429, "TooManyRequests", "Too many requests for the user")


def make_job(job_id: str, state: str, operation: str = "APPLY", age_minutes: int = 0) -> models.Job:
    return models.Job(
        id=job_id,
        stack_id=STACK_OCID,
        display_name=f"{operation.lower()}-job",
        operation=operation,
        lifecycle_state=state,
        time_created=_BASE_TIME - timedelta(minutes=age_minutes),
    )


def _summary(job: models.Job) -> models.JobSummary:
    """ListJobs returns JobSummary objects, which carry no failure_details."""
    fields = models.JobSummary().swagger_types
    return models.JobSummary(**{name: getattr(job, name) for name in fields})


class FakeResourceManagerClient:
    """Scripted ResourceManagerClient. Only the methods the app may use exist;
    calling anything else (delete, cancel, update...) raises AttributeError."""

    def __init__(
        self,
        *,
        jobs: list[models.Job] | None = None,
        attempts: list[Any] = (),
        job_states: dict[str, list[str]] | None = None,
        logs: dict[str, str] | None = None,
        errors: dict[str, list[BaseException]] | None = None,
        stack_state: str = "ACTIVE",
        events: list | None = None,
    ) -> None:
        self.events: list = events if events is not None else []
        self.stack = models.Stack(
            id=STACK_OCID,
            display_name=STACK_NAME,
            lifecycle_state=stack_state,
            compartment_id="ocid1.compartment.oc1..aaaaaaaatest",
        )
        self.jobs = list(jobs or [])  # newest first
        self.attempts = deque(attempts)  # consumed by create_job
        self.job_states = {key: deque(value) for key, value in (job_states or {}).items()}
        self.logs = dict(logs or {})
        self.errors = {key: deque(value) for key, value in (errors or {}).items()}
        self.created: list[models.CreateJobDetails] = []
        self.retry_tokens: list[str] = []

    def _enter(self, name: str, detail: Any = None) -> None:
        self.events.append((name, detail))
        pending = self.errors.get(name)
        if pending:
            raise pending.popleft()

    def get_stack(self, stack_id: str, **kwargs: Any) -> SimpleNamespace:
        self._enter("get_stack", stack_id)
        if stack_id != self.stack.id:
            raise service_error(404, "NotAuthorizedOrNotFound", "not found", "get_stack")
        return SimpleNamespace(data=self.stack)

    def list_jobs(self, **kwargs: Any) -> SimpleNamespace:
        self._enter("list_jobs", kwargs)
        assert kwargs["stack_id"] == STACK_OCID
        assert kwargs["sort_order"] == "DESC"
        return SimpleNamespace(data=[_summary(job) for job in self.jobs])

    def create_job(self, details: models.CreateJobDetails, **kwargs: Any) -> SimpleNamespace:
        self._enter("create_job", kwargs.get("opc_retry_token"))
        self.retry_tokens.append(kwargs.get("opc_retry_token"))
        outcome = self.attempts.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        self.created.append(details)
        job_id = f"ocid1.ormjob.oc1.eu-frankfurt-1.fakejob{len(self.created):06d}"
        job = make_job(job_id, "ACCEPTED")
        self.jobs.insert(0, job)
        self.job_states[job.id] = deque(["IN_PROGRESS", outcome.final_state])
        self.logs[job.id] = outcome.log
        return SimpleNamespace(data=job)

    def get_job(self, job_id: str, **kwargs: Any) -> SimpleNamespace:
        self._enter("get_job", job_id)
        job = next(j for j in self.jobs if j.id == job_id)
        states = self.job_states.get(job_id)
        if states:
            job.lifecycle_state = states.popleft() if len(states) > 1 else states[0]
        if job.lifecycle_state == "FAILED":
            job.failure_details = models.FailureDetails(
                code="TERRAFORM_EXECUTION_ERROR", message="Terraform apply failed"
            )
        return SimpleNamespace(data=job)

    def get_job_logs_content(self, job_id: str, **kwargs: Any) -> SimpleNamespace:
        self._enter("get_job_logs_content", job_id)
        return SimpleNamespace(data=self.logs.get(job_id, ""))


class FakeSleep:
    """Records requested delays into the shared event list instead of sleeping."""

    def __init__(self, events: list, interrupt_at: int | None = None) -> None:
        self.events = events
        self.delays: list[float] = []
        self.interrupt_at = interrupt_at  # raise KeyboardInterrupt on this delay value

    def __call__(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))
        self.delays.append(seconds)
        if self.interrupt_at is not None and seconds == self.interrupt_at:
            raise KeyboardInterrupt


@pytest.fixture
def settings() -> Settings:
    return Settings(
        stack_ocid=STACK_OCID, region="eu-frankfurt-1", notify_bell=False, notify_desktop=False
    )


def gateway_for(client: FakeResourceManagerClient) -> ResourceManagerGateway:
    return ResourceManagerGateway(client, region="eu-frankfurt-1")


def event_names(events: list) -> list[str]:
    return [name for name, _ in events]


@pytest.fixture
def oci_config(tmp_path: Path) -> Path:
    """A syntactically valid OCI config with a freshly generated (throwaway) RSA key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file = tmp_path / "oci_api_key.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    config_file = tmp_path / "oci_config"
    config_file.write_text(
        "[DEFAULT]\n"
        "user=ocid1.user.oc1..aaaaaaaatestuser\n"
        "fingerprint=12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef\n"
        "tenancy=ocid1.tenancy.oc1..aaaaaaaatesttenancy\n"
        "region=eu-frankfurt-1\n"
        f"key_file={key_file.as_posix()}\n",
        encoding="utf-8",
    )
    return config_file
