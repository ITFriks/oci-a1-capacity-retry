"""Pure helpers: backoff, log analysis, the next-action decision, OCID shortening."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from oci_a1_retry.analysis import is_capacity_failure, strip_control, useful_tail
from oci_a1_retry.jobs import Action, JobInfo, decide, format_time, short_ocid
from oci_a1_retry.retry import Backoff


def test_backoff_sequence_doubles_and_caps():
    backoff = Backoff(5, 60)
    delays = []
    for _ in range(8):
        delays.append(backoff.delay)
        backoff = backoff.advance()
    assert delays == [5, 10, 20, 40, 60, 60, 60, 60]


def test_backoff_reset_and_immutability():
    original = Backoff(5, 60)
    advanced = original.advance().advance()
    assert original.delay == 5
    assert advanced.delay == 20
    assert advanced.reset().delay == 5


def test_backoff_exponent_stays_bounded_after_many_advances():
    backoff = Backoff(5, 60)
    for _ in range(100_000):
        backoff = backoff.advance()
    assert backoff.delay == 60
    assert backoff.attempt < 10


@pytest.mark.parametrize(
    "text",
    [
        "Error: 500-InternalError, Out of host capacity.",
        "error: OUT OF HOST CAPACITY",
        "LaunchInstance failed: Out of capacity for shape VM.Standard.A1.Flex",
    ],
)
def test_capacity_errors_are_detected_case_insensitively(text):
    assert is_capacity_failure(text)


@pytest.mark.parametrize(
    "text",
    [
        "Error: 400-InvalidParameter, Invalid shape config",
        "Error: 400-LimitExceeded, You have reached your service limit",
        "code: TooManyRequests status: 429",
        "",
        None,
    ],
)
def test_other_errors_are_not_capacity(text):
    assert not is_capacity_failure(text)


def test_capacity_found_in_any_of_several_texts():
    assert is_capacity_failure("", None, "Out of host capacity")


def test_useful_tail_skips_blank_lines_and_strips_control_characters():
    log = "\n".join(
        [*(f"line {i}" for i in range(50)), "", "   ", "\x1b[31mError: boom\x1b[0m\x07"]
    )
    tail = useful_tail(log, limit=30)
    assert len(tail) == 30
    assert tail[-1] == "Error: boom"
    assert tail[0] == "line 21"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("plain\ttext", "plain\ttext"),
        ("\x1b[1;31mred\x1b[0m", "red"),
        ("\x1b]0;window title\x07after", "after"),
        ("\x1b]8;;http://x\x1b\\link", "link"),
        ("c1\x9bcsi\x85", "c1csi"),
        ("line\nbreak\r", "linebreak"),
    ],
)
def test_strip_control(raw, clean):
    assert strip_control(raw) == clean


def job(state, operation="APPLY", job_id="ocid1.ormjob.oc1..x"):
    return JobInfo(id=job_id, operation=operation, lifecycle_state=state)


@pytest.mark.parametrize(
    ("jobs", "force", "expected"),
    [
        ([], False, Action.CREATE),
        ([job("FAILED")], False, Action.CREATE),
        ([job("CANCELED")], False, Action.CREATE),
        ([job("SUCCEEDED")], False, Action.NOTHING_TO_DO),
        ([job("SUCCEEDED")], True, Action.CREATE),
        ([job("IN_PROGRESS")], True, Action.ATTACH),
        ([job("ACCEPTED")], False, Action.ATTACH),
        ([job("CANCELING")], False, Action.ATTACH),
        ([job("IN_PROGRESS", "PLAN")], False, Action.WAIT_FOR_OTHER),
        ([job("SUCCEEDED", "DESTROY"), job("SUCCEEDED")], False, Action.BLOCKED_BY_DESTROY),
        ([job("SUCCEEDED", "DESTROY")], True, Action.CREATE),
        ([job("SUCCEEDED", "PLAN"), job("SUCCEEDED")], False, Action.NOTHING_TO_DO),
        ([job("SUCCEEDED", "PLAN"), job("FAILED")], False, Action.CREATE),
        ([job("FAILED"), job("SUCCEEDED")], False, Action.CREATE),
    ],
)
def test_decide(jobs, force, expected):
    assert decide(jobs, force=force).action is expected


def test_short_ocid():
    ocid = "ocid1.ormjob.oc1.eu-frankfurt-1.amaaaaaaabcdefxyz123"
    assert short_ocid(ocid) == "ocid1.ormjob...xyz123"
    assert short_ocid(ocid, full=True) == ocid
    assert short_ocid("not-an-ocid") == "not-an-ocid"


def test_format_time():
    assert format_time(None) == "-"
    assert format_time(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)).startswith("2026-01-0")
