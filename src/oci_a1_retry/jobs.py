"""Resource Manager job/stack views and the pure "what should happen next" decision."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

ACCEPTED = "ACCEPTED"
IN_PROGRESS = "IN_PROGRESS"
CANCELING = "CANCELING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELED = "CANCELED"
ACTIVE_STATES = frozenset({ACCEPTED, IN_PROGRESS, CANCELING})

APPLY = "APPLY"
DESTROY = "DESTROY"


@dataclass(frozen=True)
class StackInfo:
    id: str
    display_name: str
    lifecycle_state: str
    compartment_id: str = ""


@dataclass(frozen=True)
class JobInfo:
    id: str
    operation: str
    lifecycle_state: str
    time_created: datetime | None = None
    time_finished: datetime | None = None
    display_name: str = ""
    failure_code: str = ""
    failure_message: str = ""

    @property
    def is_active(self) -> bool:
        return self.lifecycle_state in ACTIVE_STATES


class Action(Enum):
    ATTACH = "attach"  # an Apply job is already running: monitor it, never create another
    WAIT_FOR_OTHER = "wait_for_other"  # a Plan/Destroy/... job holds the stack lock
    NOTHING_TO_DO = "nothing_to_do"  # the latest Apply succeeded
    BLOCKED_BY_DESTROY = "blocked_by_destroy"  # the latest relevant job is a Destroy
    CREATE = "create"


@dataclass(frozen=True)
class Decision:
    action: Action
    job: JobInfo | None = None


def decide(jobs: Sequence[JobInfo], *, force: bool) -> Decision:
    """Decide the next step from the stack's jobs (newest first).

    A running job always wins, even with ``force``: two Apply jobs are never
    started on purpose. Without ``force`` a succeeded Apply means "done", and a
    Destroy newer than any Apply means the user deliberately removed the
    resources, so nothing is re-created automatically.
    """
    active = next((job for job in jobs if job.is_active), None)
    if active is not None:
        action = Action.ATTACH if active.operation == APPLY else Action.WAIT_FOR_OTHER
        return Decision(action, active)
    if force:
        return Decision(Action.CREATE)
    latest = next((job for job in jobs if job.operation in (APPLY, DESTROY)), None)
    if latest is None:
        return Decision(Action.CREATE)
    if latest.operation == DESTROY:
        return Decision(Action.BLOCKED_BY_DESTROY, latest)
    if latest.lifecycle_state == SUCCEEDED:
        return Decision(Action.NOTHING_TO_DO, latest)
    return Decision(Action.CREATE, latest)


def short_ocid(ocid: str, *, full: bool = False) -> str:
    """'ocid1.ormjob.oc1.eu-frankfurt-1.aaaa...xyz123' -> 'ocid1.ormjob...xyz123'."""
    if full or not ocid.startswith("ocid1.") or ocid.count(".") < 2:
        return ocid
    return f"ocid1.{ocid.split('.')[1]}...{ocid[-6:]}"


def format_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")
