"""End-to-end CLI behaviour with a fake gateway (no network)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fakes import (
    CAPACITY_LOG,
    OTHER_FAILURE_LOG,
    STACK_NAME,
    STACK_OCID,
    Attempt,
    FakeResourceManagerClient,
    FakeSleep,
    gateway_for,
    make_job,
    service_error,
)

from oci_a1_retry import __version__
from oci_a1_retry.cli import main
from oci_a1_retry.logging_setup import LOGGER_NAME
from oci_a1_retry.oci_client import OciSetupError


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate every test from the developer's real config, env vars and log dir."""
    for name in ("OCI_A1_STACK_OCID", "OCI_A1_REGION", "OCI_A1_PROFILE", "OCI_A1_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("OCI_A1_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OCI_A1_NOTIFY_BELL", "false")
    monkeypatch.setenv("OCI_A1_NOTIFY_DESKTOP", "false")
    yield tmp_path
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def cli(args, client, sleep=None):
    sleep = sleep or FakeSleep(client.events)
    return main(
        [*args, "--stack-id", STACK_OCID, "--region", "eu-frankfurt-1"],
        gateway_factory=lambda settings: gateway_for(client),
        sleep=sleep,
    )


def log_text(env: Path) -> str:
    return (env / "logs" / "oci-a1-retry.log").read_text(encoding="utf-8")


def test_no_subcommand_prints_help(capsys):
    assert main([]) == 2
    assert "doctor" in capsys.readouterr().out


def test_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_run_success_prints_banner_and_exits_zero(env, capsys):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", CAPACITY_LOG), Attempt()])

    assert cli(["run"], client) == 0

    out = capsys.readouterr().out
    assert "OCI A1 Capacity Retry" in out
    assert f"Stack:        {STACK_NAME}" in out
    assert "Retry delay:  300 seconds" in out
    assert "No A1 host capacity available." in out
    assert "SUCCESS" in out
    assert "Retry loop stopped." in out
    assert STACK_OCID not in out  # shortened in normal output
    log = log_text(env)
    assert "SUCCESS" in log
    assert "ocid1.ormjob.oc1.eu-frankfurt-1.fakejob000002" in log  # full job OCID only in the file


def test_verbose_shows_full_ocids(env, capsys):
    client = FakeResourceManagerClient(attempts=[Attempt()])
    assert cli(["run", "-v"], client) == 0
    assert STACK_OCID in capsys.readouterr().out


def test_once_reports_no_capacity_with_exit_code_4(env):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", CAPACITY_LOG)])
    assert cli(["once"], client) == 4
    assert len(client.created) == 1


def test_unexpected_failure_exits_one(env, capsys):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", OTHER_FAILURE_LOG)])
    assert cli(["run"], client) == 1
    out = capsys.readouterr().out
    assert "STOPPED" in out
    assert "Invalid shape config" in out


def test_nothing_to_do_exits_zero_without_creating(env, capsys):
    client = FakeResourceManagerClient(jobs=[make_job("ocid1.ormjob.oc1..done", "SUCCEEDED")])
    assert cli(["run"], client) == 0
    assert client.created == []
    assert "Nothing to do." in capsys.readouterr().out


def test_force_flag_reaches_the_runner(env):
    client = FakeResourceManagerClient(
        jobs=[make_job("ocid1.ormjob.oc1..done", "SUCCEEDED")], attempts=[Attempt()]
    )
    assert cli(["run", "--force"], client) == 0
    assert len(client.created) == 1


def test_ctrl_c_stops_cleanly_without_new_jobs(env, capsys):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", CAPACITY_LOG), Attempt()])

    code = cli(["run"], client, sleep=FakeSleep(client.events, interrupt_at=300))

    assert code == 130
    assert len(client.created) == 1
    assert "Retry loop stopped by user." in capsys.readouterr().out


def test_ctrl_c_before_any_job_mentions_nothing_was_canceled(env, capsys):
    client = FakeResourceManagerClient(attempts=[Attempt("FAILED", CAPACITY_LOG), Attempt()])
    assert cli(["run"], client, sleep=FakeSleep(client.events, interrupt_at=300)) == 130
    assert "Nothing was canceled in OCI" in capsys.readouterr().out


def test_unexpected_error_gives_a_message_not_a_traceback(env, capsys):
    client = FakeResourceManagerClient(errors={"list_jobs": [RuntimeError("weird SDK state")]})

    assert cli(["run"], client) == 1

    out = capsys.readouterr().out
    assert "Unexpected error: RuntimeError: weird SDK state" in out
    assert "Traceback" not in out
    assert "Traceback" in log_text(env)


def test_header_shows_success_command(env, capsys, monkeypatch):
    monkeypatch.setenv("OCI_A1_SUCCESS_COMMAND", "echo done")
    client = FakeResourceManagerClient(jobs=[make_job("ocid1.ormjob.oc1..done", "SUCCEEDED")])
    assert cli(["run"], client) == 0
    assert "Success cmd:  echo done" in capsys.readouterr().out


def test_ctrl_c_while_monitoring_says_job_keeps_running(env, capsys):
    client = FakeResourceManagerClient(attempts=[Attempt()])

    code = cli(["run"], client, sleep=FakeSleep(client.events, interrupt_at=15))

    out = capsys.readouterr().out
    assert code == 130
    assert "Retry loop stopped by user." in out
    assert "was NOT canceled" in out


def test_authentication_failure_exits_three(env, capsys):
    client = FakeResourceManagerClient(
        errors={"get_stack": [service_error(401, "NotAuthenticated", "bad", "get_stack")]}
    )
    assert cli(["run"], client) == 3
    assert "API credentials" in capsys.readouterr().out
    assert client.created == []


def test_missing_oci_setup_exits_three(env, capsys):
    def factory(settings):
        raise OciSetupError("OCI config file not found")

    code = main(["run", "--stack-id", STACK_OCID], gateway_factory=factory, sleep=FakeSleep([]))

    assert code == 3
    assert "OCI config file not found" in capsys.readouterr().out


def test_real_factory_with_missing_oci_config_exits_three(env, capsys):
    code = main(["status", "--stack-id", STACK_OCID, "--oci-config-file", str(env / "missing")])
    assert code == 3
    assert "oci setup config" in capsys.readouterr().out


def test_permanent_create_error_exits_three(env):
    client = FakeResourceManagerClient(
        attempts=[service_error(400, "InvalidParameter", "bad", "create_job")]
    )
    assert cli(["run"], client) == 3


def test_invalid_configuration_exits_two(env, capsys):
    code = main(["run", "--stack-id", "ocid1.instance.oc1..x"], gateway_factory=None)
    assert code == 2
    assert "'instance' OCID" in capsys.readouterr().err


def test_broken_config_file_exits_two(env, capsys):
    (env / "config.toml").write_text("capacity_retry_seconds = 'x'\n", encoding="utf-8")
    assert main(["run"]) == 2
    assert "must be an integer" in capsys.readouterr().err


def test_gave_up_exits_five(env, monkeypatch):
    monkeypatch.setattr("oci_a1_retry.runner.MAX_CONSECUTIVE_ERRORS", 2)
    client = FakeResourceManagerClient(
        errors={"list_jobs": [service_error(503, "Unavailable", "x", "list_jobs")] * 3}
    )
    assert cli(["run"], client) == 5


def test_status_human_output_is_read_only(env, capsys):
    client = FakeResourceManagerClient(
        jobs=[make_job("ocid1.ormjob.oc1.eu-frankfurt-1.failedjob1", "FAILED")]
    )
    assert cli(["status"], client) == 0
    out = capsys.readouterr().out
    assert STACK_NAME in out
    assert "FAILED" in out
    assert "would submit a new Apply job" in out
    assert "create_job" not in [name for name, _ in client.events]


def test_status_json(env, capsys):
    running = make_job("ocid1.ormjob.oc1.eu-frankfurt-1.running", "IN_PROGRESS")
    client = FakeResourceManagerClient(jobs=[running])

    assert cli(["status", "--json"], client) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["stack"]["display_name"] == STACK_NAME
    assert payload["next_action"] == "attach"
    assert payload["active_job"]["id"] == running.id
    assert payload["region"] == "eu-frankfurt-1"


def test_doctor_all_checks_pass(env, oci_config, capsys):
    client = FakeResourceManagerClient(jobs=[make_job("ocid1.ormjob.oc1..x", "FAILED")])

    code = cli(["doctor", "--oci-config-file", str(oci_config)], client)

    out = capsys.readouterr().out
    assert code == 0, out
    assert "[FAIL]" not in out
    assert f"stack '{STACK_NAME}' is ACTIVE" in out
    assert "latest Apply FAILED" in out


def test_doctor_reports_missing_stack_and_oci_config(env, capsys):
    code = main(["doctor", "--oci-config-file", str(env / "missing")])
    out = capsys.readouterr().out
    assert code == 1
    assert "stack_ocid is not set" in out
    assert "OCI config file not found" in out


def test_doctor_reports_api_permission_problem(env, oci_config, capsys):
    client = FakeResourceManagerClient(
        errors={"get_stack": [service_error(404, "NotAuthorizedOrNotFound", "x", "get_stack")]}
    )
    code = cli(["doctor", "--oci-config-file", str(oci_config)], client)
    assert code == 1
    assert "manage orm-jobs" in capsys.readouterr().out
