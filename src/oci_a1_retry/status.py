"""`status`: read-only view of the stack, its recent jobs and what `run` would do."""

from __future__ import annotations

import json
from typing import Any

from .config import Settings
from .jobs import Action, Decision, JobInfo, StackInfo, decide, format_time, short_ocid
from .oci_client import ResourceManagerGateway
from .runner import EXIT_OK

RECENT_JOBS = 5

NEXT_ACTION_TEXT = {
    Action.ATTACH: "'run' would monitor the running Apply job (no new job is created).",
    Action.WAIT_FOR_OTHER: "'run' would wait for the running job to finish, then decide.",
    Action.NOTHING_TO_DO: "Latest Apply succeeded: nothing to do ('run --force' to re-apply).",
    Action.BLOCKED_BY_DESTROY: "Latest job is a DESTROY: 'run' will not re-apply without --force.",
    Action.CREATE: "'run' would submit a new Apply job.",
}


def run_status(
    gateway: ResourceManagerGateway, settings: Settings, *, as_json: bool, verbose: bool
) -> int:
    stack = gateway.get_stack(settings.stack_ocid)
    jobs = gateway.list_jobs(settings.stack_ocid)
    decision = decide(jobs, force=False)
    if as_json:
        payload = {
            "region": gateway.region,
            "profile": settings.profile,
            "stack": {
                "id": stack.id,
                "display_name": stack.display_name,
                "lifecycle_state": stack.lifecycle_state,
            },
            "next_action": decision.action.value,
            "active_job": _job_dict(next((j for j in jobs if j.is_active), None)),
            "latest_apply_job": _job_dict(next((j for j in jobs if j.operation == "APPLY"), None)),
            "recent_jobs": [_job_dict(job) for job in jobs[:RECENT_JOBS]],
        }
        print(json.dumps(payload, indent=2))
        return EXIT_OK
    _print_human(gateway, settings, stack, jobs, decision, verbose)
    return EXIT_OK


def _print_human(
    gateway: ResourceManagerGateway,
    settings: Settings,
    stack: StackInfo,
    jobs: list[JobInfo],
    decision: Decision,
    verbose: bool,
) -> None:
    print("OCI A1 Capacity Retry - status")
    print("------------------------------")
    print(f"Region:       {gateway.region}")
    print(f"Profile:      {settings.profile}")
    print(f"Stack:        {stack.display_name} ({stack.lifecycle_state})")
    print(f"Stack OCID:   {short_ocid(stack.id, full=verbose)}")
    print()
    if not jobs:
        print("No jobs yet.")
    else:
        print("Recent jobs (newest first):")
        for job in jobs[:RECENT_JOBS]:
            print(
                f"  {format_time(job.time_created)}  {job.operation:<9} "
                f"{job.lifecycle_state:<11} {short_ocid(job.id, full=verbose)}"
            )
    print()
    print(f"Next action:  {NEXT_ACTION_TEXT[decision.action]}")


def _job_dict(job: JobInfo | None) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "id": job.id,
        "operation": job.operation,
        "lifecycle_state": job.lifecycle_state,
        "display_name": job.display_name,
        "time_created": job.time_created.isoformat() if job.time_created else None,
        "time_finished": job.time_finished.isoformat() if job.time_finished else None,
    }
