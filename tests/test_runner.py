"""The retry loop against a fake Resource Manager (no network)."""

from __future__ import annotations

import logging
from collections import deque
from types import SimpleNamespace

import pytest
from fakes import (
    CAPACITY_LOG,
    OTHER_FAILURE_LOG,
    STACK_OCID,
    Attempt,
    FakeResourceManagerClient,
    FakeSleep,
    event_names,
    gateway_for,
    make_job,
    service_error,
    throttled,
)

from oci_a1_retry import runner as runner_module
from oci_a1_retry.oci_client import ErrorKind, OciCallError
from oci_a1_retry.runner import GaveUp, Outcome, Runner

POLL = 15
CAPACITY_WAIT = 300


def run(client, settings, *, loop=True, force=False, sleep=None, on_success=None):
    sleep = sleep or FakeSleep(client.events)
    runner = Runner(gateway_for(client), settings, sleep=sleep, on_success=on_success)
    outcome = runner.run(runner.load_stack(), loop=loop, force=force)
    return outcome, sleep, runner


def non_poll_sleeps(sleep: FakeSleep) -> list[float]:
    return [d for d in sleep.delays if d != POLL]


def test_apply_success_stops_with_exit_code_zero(settings):
    client = FakeResourceManagerClient(attempts=[Attempt("SUCCEEDED")])
    seen = []

    outcome, sleep, runner = run(client, settings, on_success=lambda s, j: seen.append(j.id))

    assert outcome is Outcome.APPLIED
    assert outcome.exit_code == 0
    assert len(client.created) == 1
    assert seen == [client.jobs[0].id]
    assert runner.jobs_created == 1
    assert non_poll_sleeps(sleep) == []


def test_created_job_is_auto_approved_apply_for_the_configured_stack(settings):
    client = FakeResourceManagerClient(attempts=[Attempt("SUCCEEDED")])

    run(client, settings)

    details = client.created[0]
    assert details.stack_id == STACK_OCID
    assert details.job_operation_details.operation == "APPLY"
    assert details.job_operation_details.execution_plan_strategy == "AUTO_APPROVED"


def test_capacity_failure_waits_then_submits_another_attempt(settings, caplog):
    client = FakeResourceManagerClient(
        attempts=[Attempt("FAILED", CAPACITY_LOG), Attempt("FAILED", CAPACITY_LOG), Attempt()]
    )

    with caplog.at_level(logging.INFO, logger="oci_a1_retry"):
        outcome, sleep, _ = run(client, settings)

    assert outcome is Outcome.APPLIED
    assert len(client.created) == 3
    assert non_poll_sleeps(sleep) == [CAPACITY_WAIT, CAPACITY_WAIT]
    assert "No A1 host capacity available." in caplog.text
    assert "Next capacity attempt in 300 seconds." in caplog.text


def test_capacity_wait_happens_before_the_next_create(settings):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", CAPACITY_LOG), Attempt()])

    run(client, settings)

    names = event_names(client.events)
    wait_index = client.events.index(("sleep", CAPACITY_WAIT))
    assert names.index("create_job") < wait_index
    assert names.index("create_job", wait_index) > wait_index


def test_create_job_429_backs_off_5_10_20_40_60_60_then_proceeds(settings, caplog):
    client = FakeResourceManagerClient(attempts=[throttled()] * 7 + [Attempt()])

    with caplog.at_level(logging.INFO, logger="oci_a1_retry"):
        outcome, sleep, _ = run(client, settings)

    assert outcome is Outcome.APPLIED
    assert non_poll_sleeps(sleep) == [5, 10, 20, 40, 60, 60, 60]
    assert "Resource Manager returned HTTP 429 TooManyRequests." in caplog.text
    assert "Retrying CreateJob in 5 seconds." in caplog.text
    assert len(client.created) == 1  # 429 never counted as a capacity failure


def test_429_reuses_retry_token_and_new_attempt_gets_new_token(settings):
    client = FakeResourceManagerClient(
        attempts=[throttled(), throttled(), Attempt("FAILED", CAPACITY_LOG), Attempt()]
    )

    run(client, settings)

    first, second, third, fourth = client.retry_tokens
    assert first == second == third  # same logical attempt: idempotent re-send
    assert fourth != first


def test_throttle_delay_resets_after_successful_create(settings):
    client = FakeResourceManagerClient(
        attempts=[
            throttled(),
            throttled(),
            throttled(),
            Attempt("FAILED", CAPACITY_LOG),
            throttled(),
            Attempt(),
        ]
    )

    _, sleep, _ = run(client, settings)

    assert non_poll_sleeps(sleep) == [5, 10, 20, CAPACITY_WAIT, 5]


def test_unexpected_terraform_error_stops_and_shows_log_tail(settings, caplog):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", OTHER_FAILURE_LOG)])

    with caplog.at_level(logging.INFO, logger="oci_a1_retry"):
        outcome, sleep, _ = run(client, settings)

    assert outcome is Outcome.APPLY_FAILED
    assert outcome.exit_code == 1
    assert len(client.created) == 1
    assert CAPACITY_WAIT not in sleep.delays
    assert "Apply failed for a reason other than host capacity." in caplog.text
    assert "Invalid shape config" in caplog.text
    assert "STOPPED" in caplog.text


def test_capacity_detected_from_failure_details_when_log_unavailable(settings):
    client = FakeResourceManagerClient(
        attempts=[Attempt("FAILED", ""), Attempt()],
        errors={"get_job_logs_content": [service_error(404, "NotFound", "gone", "get_job_logs")]},
    )
    original_get_job = client.get_job

    def get_job_with_capacity_message(job_id, **kwargs):
        response = original_get_job(job_id, **kwargs)
        if response.data.lifecycle_state == "FAILED":
            response.data.failure_details.message = "Out of host capacity."
        return response

    client.get_job = get_job_with_capacity_message

    outcome, _, _ = run(client, settings)

    assert outcome is Outcome.APPLIED
    assert len(client.created) == 2


def test_capacity_log_that_appears_late_is_still_recognised(settings):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", CAPACITY_LOG), Attempt()])
    original = client.get_job_logs_content
    calls = []

    def log_lags_behind(job_id, **kwargs):
        calls.append(job_id)
        response = original(job_id, **kwargs)
        return SimpleNamespace(data="") if len(calls) < 3 else response

    client.get_job_logs_content = log_lags_behind

    outcome, sleep, _ = run(client, settings)

    assert outcome is Outcome.APPLIED
    assert len(client.created) == 2
    assert non_poll_sleeps(sleep) == [CAPACITY_WAIT]


def test_unreadable_log_without_capacity_details_stops_safely(settings, caplog):
    client = FakeResourceManagerClient(
        attempts=[Attempt("FAILED", CAPACITY_LOG)],
        errors={"get_job_logs_content": [service_error(404, "NotFound", "x", "get_job_logs")] * 3},
    )

    with caplog.at_level(logging.INFO, logger="oci_a1_retry"):
        outcome, _, _ = run(client, settings)

    assert outcome is Outcome.APPLY_FAILED
    assert event_names(client.events).count("get_job_logs_content") == 3
    assert "Job log unavailable" in caplog.text


def test_running_apply_job_is_monitored_instead_of_creating(settings, caplog):
    existing = make_job("ocid1.ormjob.oc1.eu-frankfurt-1.otherhostjob", "IN_PROGRESS")
    client = FakeResourceManagerClient(
        jobs=[existing], job_states={existing.id: ["IN_PROGRESS", "SUCCEEDED"]}
    )

    with caplog.at_level(logging.INFO, logger="oci_a1_retry"):
        outcome, _, _ = run(client, settings)

    assert outcome is Outcome.APPLIED
    assert "create_job" not in event_names(client.events)
    assert "monitoring it instead of creating a new one" in caplog.text


@pytest.mark.parametrize("state", ["ACCEPTED", "IN_PROGRESS", "CANCELING"])
def test_any_active_apply_state_blocks_creation_even_with_force(settings, state):
    existing = make_job("ocid1.ormjob.oc1.eu-frankfurt-1.activejob", state)
    client = FakeResourceManagerClient(jobs=[existing], job_states={existing.id: ["SUCCEEDED"]})

    run(client, settings, force=True)

    assert "create_job" not in event_names(client.events)


def test_attached_job_failing_with_capacity_leads_to_new_attempt(settings):
    existing = make_job("ocid1.ormjob.oc1.eu-frankfurt-1.activejob", "IN_PROGRESS")
    client = FakeResourceManagerClient(
        jobs=[existing],
        job_states={existing.id: ["FAILED"]},
        logs={existing.id: CAPACITY_LOG},
        attempts=[Attempt()],
    )

    outcome, _, _ = run(client, settings)

    assert outcome is Outcome.APPLIED
    assert len(client.created) == 1


def test_latest_apply_succeeded_means_nothing_to_do(settings, caplog):
    client = FakeResourceManagerClient(
        jobs=[make_job("ocid1.ormjob.oc1.eu-frankfurt-1.donejob", "SUCCEEDED")]
    )

    with caplog.at_level(logging.INFO, logger="oci_a1_retry"):
        outcome, _, _ = run(client, settings)

    assert outcome is Outcome.NOTHING_TO_DO
    assert outcome.exit_code == 0
    assert "create_job" not in event_names(client.events)
    assert "The latest Apply job succeeded" in caplog.text
    assert "Nothing to do." in caplog.text


def test_force_overrides_previous_success(settings):
    client = FakeResourceManagerClient(
        jobs=[make_job("ocid1.ormjob.oc1.eu-frankfurt-1.donejob", "SUCCEEDED")],
        attempts=[Attempt()],
    )

    outcome, _, _ = run(client, settings, force=True)

    assert outcome is Outcome.APPLIED
    assert len(client.created) == 1


def test_success_from_another_machine_during_capacity_wait_stops_loop(settings):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", CAPACITY_LOG)])
    sleep = FakeSleep(client.events)

    def sleep_and_let_other_machine_succeed(seconds):
        sleep(seconds)
        if seconds == CAPACITY_WAIT:
            client.jobs.insert(0, make_job("ocid1.ormjob.oc1.eu-frankfurt-1.otherpc", "SUCCEEDED"))

    outcome, _, _ = run(client, settings, sleep=sleep_and_let_other_machine_succeed)

    assert outcome is Outcome.NOTHING_TO_DO
    assert len(client.created) == 1


def test_latest_destroy_blocks_automatic_reapply(settings):
    client = FakeResourceManagerClient(
        jobs=[
            make_job("ocid1.ormjob.oc1.eu-frankfurt-1.destroy", "SUCCEEDED", "DESTROY"),
            make_job("ocid1.ormjob.oc1.eu-frankfurt-1.apply", "SUCCEEDED", age_minutes=60),
        ]
    )

    outcome, _, _ = run(client, settings)

    assert outcome is Outcome.BLOCKED
    assert "create_job" not in event_names(client.events)


def test_running_plan_job_is_waited_for_then_apply_created(settings):
    plan = make_job("ocid1.ormjob.oc1.eu-frankfurt-1.plan", "IN_PROGRESS", "PLAN")
    client = FakeResourceManagerClient(
        jobs=[plan], job_states={plan.id: ["SUCCEEDED"]}, attempts=[Attempt()]
    )

    outcome, _, _ = run(client, settings)

    names = event_names(client.events)
    assert outcome is Outcome.APPLIED
    assert names.index("get_job") < names.index("create_job")


def test_once_mode_reports_no_capacity_without_waiting(settings):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", CAPACITY_LOG), Attempt()])

    outcome, sleep, _ = run(client, settings, loop=False)

    assert outcome is Outcome.NO_CAPACITY
    assert outcome.exit_code == 4
    assert len(client.created) == 1
    assert CAPACITY_WAIT not in sleep.delays


def test_canceled_job_stops_loop(settings):
    client = FakeResourceManagerClient(attempts=[Attempt("CANCELED")])

    outcome, _, _ = run(client, settings)

    assert outcome is Outcome.CANCELED
    assert outcome.exit_code == 1


def test_transient_poll_error_is_retried_with_backoff(settings):
    client = FakeResourceManagerClient(
        attempts=[Attempt()],
        errors={"get_job": [service_error(503, "ServiceUnavailable", "down", "get_job")] * 2},
    )

    outcome, sleep, _ = run(client, settings)

    assert outcome is Outcome.APPLIED
    assert non_poll_sleeps(sleep) == [5, 10]


def test_transient_create_error_rechecks_jobs_before_resubmitting(settings):
    client = FakeResourceManagerClient(
        attempts=[service_error(502, "BadGateway", "proxy", "create_job"), Attempt()]
    )

    outcome, _, _ = run(client, settings)

    names = event_names(client.events)
    first_create = names.index("create_job")
    assert outcome is Outcome.APPLIED
    assert "list_jobs" in names[first_create : names.index("create_job", first_create + 1)]
    assert client.retry_tokens[0] == client.retry_tokens[1]


def test_conflict_on_create_rediscovers_and_attaches(settings):
    client = FakeResourceManagerClient(
        attempts=[service_error(409, "Conflict", "stack is locked", "create_job")]
    )
    other = make_job("ocid1.ormjob.oc1.eu-frankfurt-1.otherpc", "IN_PROGRESS")
    client.job_states[other.id] = deque(["SUCCEEDED"])
    original_create = client.create_job

    def create_while_other_machine_wins(details, **kwargs):
        client.jobs.insert(0, other)
        return original_create(details, **kwargs)

    client.create_job = create_while_other_machine_wins

    outcome, _, _ = run(client, settings)

    assert outcome is Outcome.APPLIED
    assert len(client.created) == 0


def test_gives_up_after_too_many_consecutive_transient_errors(settings, monkeypatch):
    monkeypatch.setattr(runner_module, "MAX_CONSECUTIVE_ERRORS", 3)
    client = FakeResourceManagerClient(
        errors={"list_jobs": [service_error(500, "InternalError", "x", "list_jobs")] * 5}
    )

    with pytest.raises(GaveUp):
        run(client, settings)

    assert "create_job" not in event_names(client.events)


def test_permanent_error_is_not_retried(settings):
    client = FakeResourceManagerClient(
        attempts=[service_error(400, "InvalidParameter", "bad request", "create_job")]
    )

    with pytest.raises(OciCallError) as excinfo:
        run(client, settings)

    assert excinfo.value.kind is ErrorKind.PERMANENT
    assert event_names(client.events).count("create_job") == 1


def test_authentication_failure_stops_immediately(settings):
    client = FakeResourceManagerClient(
        errors={"get_stack": [service_error(401, "NotAuthenticated", "bad sig", "get_stack")]}
    )

    with pytest.raises(OciCallError) as excinfo:
        run(client, settings)

    assert excinfo.value.kind is ErrorKind.AUTH
    assert event_names(client.events) == ["get_stack"]


def test_inactive_stack_is_rejected(settings):
    client = FakeResourceManagerClient(stack_state="DELETED")
    runner = Runner(gateway_for(client), settings, sleep=FakeSleep(client.events))

    with pytest.raises(OciCallError, match="DELETED"):
        runner.load_stack()


def test_ctrl_c_during_capacity_wait_submits_nothing_more(settings):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", CAPACITY_LOG), Attempt()])
    sleep = FakeSleep(client.events, interrupt_at=CAPACITY_WAIT)

    with pytest.raises(KeyboardInterrupt):
        run(client, settings, sleep=sleep)

    assert len(client.created) == 1
