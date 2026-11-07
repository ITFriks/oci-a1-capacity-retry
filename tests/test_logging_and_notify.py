"""Logging (rotation, redaction, no key leaks) and optional success notifications."""

from __future__ import annotations

import io
import logging
import re
import subprocess
import sys
from logging.handlers import RotatingFileHandler

import pytest
from fakes import STACK_OCID, Attempt, FakeResourceManagerClient, FakeSleep, gateway_for

from oci_a1_retry import notify
from oci_a1_retry.cli import main
from oci_a1_retry.config import Settings
from oci_a1_retry.jobs import JobInfo, StackInfo
from oci_a1_retry.logging_setup import LOG_BACKUPS, LOGGER_NAME, MAX_LOG_BYTES, setup_logging
from oci_a1_retry.oci_client import OciSetupError, build_gateway

FAKE_KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCsecretsecret"
FAKE_PEM = f"-----BEGIN PRIVATE KEY-----\n{FAKE_KEY_BODY}\n-----END PRIVATE KEY-----\n"


@pytest.fixture(autouse=True)
def reset_logger():
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_file_log_rotates_and_has_timestamps(tmp_path):
    log_file = setup_logging(verbose=False, log_dir=tmp_path, stream=io.StringIO())
    logging.getLogger(LOGGER_NAME).info("hello")
    handler = next(
        h for h in logging.getLogger(LOGGER_NAME).handlers if isinstance(h, RotatingFileHandler)
    )
    assert handler.maxBytes == MAX_LOG_BYTES
    assert handler.backupCount == LOG_BACKUPS
    line = log_file.read_text(encoding="utf-8").strip()
    assert line.endswith("INFO    hello")
    assert line[:4].isdigit()  # date first


def test_debug_goes_to_file_but_not_to_console_unless_verbose(tmp_path):
    console = io.StringIO()
    log_file = setup_logging(verbose=False, log_dir=tmp_path, stream=console)
    logging.getLogger(LOGGER_NAME).debug("full ocid detail")
    assert "full ocid detail" in log_file.read_text(encoding="utf-8")
    assert "full ocid detail" not in console.getvalue()


def test_console_lines_have_a_short_timestamp_in_a_terminal(monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    console = io.StringIO()
    setup_logging(verbose=False, log_dir=None, stream=console)
    logging.getLogger(LOGGER_NAME).info("hello")
    assert re.fullmatch(r"\d\d:\d\d:\d\d hello\n", console.getvalue())


def test_console_omits_timestamp_under_systemd_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("JOURNAL_STREAM", "8:123456")  # set by systemd for journal output
    console = io.StringIO()
    log_file = setup_logging(verbose=False, log_dir=tmp_path, stream=console)
    logging.getLogger(LOGGER_NAME).info("hello")
    assert console.getvalue() == "hello\n"  # the journal adds its own timestamp
    assert log_file.read_text(encoding="utf-8")[:4].isdigit()  # file keeps full timestamps


def test_unwritable_log_dir_falls_back_to_console(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    console = io.StringIO()
    assert setup_logging(verbose=False, log_dir=blocker / "sub", stream=console) is None
    assert "logging to console only" in console.getvalue()


def test_private_key_material_is_redacted_everywhere(tmp_path):
    console = io.StringIO()
    log_file = setup_logging(verbose=True, log_dir=tmp_path, stream=console)
    logger = logging.getLogger(LOGGER_NAME)
    logger.error("oops %s", FAKE_PEM)
    logger.error("pass_phrase = hunter2 and security_token: abc.def")
    for text in (console.getvalue(), log_file.read_text(encoding="utf-8")):
        assert FAKE_KEY_BODY not in text
        assert "hunter2" not in text
        assert "abc.def" not in text
        assert "<redacted private key>" in text


def test_tracebacks_are_redacted_too(tmp_path):
    log_file = setup_logging(verbose=True, log_dir=tmp_path, stream=io.StringIO())
    try:
        raise ValueError(FAKE_PEM)
    except ValueError:
        logging.getLogger(LOGGER_NAME).debug("failure", exc_info=True)
    text = log_file.read_text(encoding="utf-8")
    assert "Traceback" in text
    assert FAKE_KEY_BODY not in text


def test_invalid_key_file_error_does_not_echo_key_contents(tmp_path):
    key_file = tmp_path / "key.pem"
    key_file.write_text(FAKE_PEM, encoding="utf-8")  # not a real key: fails to load
    config = tmp_path / "config"
    config.write_text(
        "[DEFAULT]\nuser=ocid1.user.oc1..aaaa\n"
        "fingerprint=12:34:56:78:90:ab:cd:ef:12:34:56:78:90:ab:cd:ef\n"
        "tenancy=ocid1.tenancy.oc1..aaaa\nregion=eu-frankfurt-1\n"
        f"key_file={key_file.as_posix()}\n",
        encoding="utf-8",
    )
    with pytest.raises(OciSetupError) as excinfo:
        build_gateway(Settings(stack_ocid=STACK_OCID, oci_config_file=str(config)))
    assert FAKE_KEY_BODY not in str(excinfo.value)
    assert "key_file" in str(excinfo.value)


def test_run_and_doctor_never_output_the_private_key(tmp_path, oci_config, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("XDG_CONFIG_HOME", "APPDATA"):  # never read the developer's real config
        monkeypatch.setenv(name, str(tmp_path / "user-config"))
    monkeypatch.setenv("OCI_A1_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("OCI_A1_NOTIFY_BELL", "false")
    monkeypatch.setenv("OCI_A1_NOTIFY_DESKTOP", "false")
    key_text = (tmp_path / "oci_api_key.pem").read_text(encoding="utf-8")
    key_body = key_text.splitlines()[1]
    client = FakeResourceManagerClient(attempts=[Attempt()])
    common = ["--stack-id", STACK_OCID, "--oci-config-file", str(oci_config), "-v"]

    def factory(settings):
        build_gateway(settings)  # really loads the key, then use the fake for API calls
        return gateway_for(client)

    assert main(["doctor", *common], gateway_factory=factory) == 0
    assert main(["run", *common], gateway_factory=factory, sleep=FakeSleep([])) == 0

    captured = capsys.readouterr()
    log_text = (tmp_path / "logs" / "oci-a1-retry.log").read_text(encoding="utf-8")
    for text in (captured.out, captured.err, log_text):
        assert key_body not in text
        assert "PRIVATE KEY" not in text
        assert "12:34:56:78:90:ab" not in text  # fingerprint not echoed either


# -- notifications -----------------------------------------------------------

STACK = StackInfo(id=STACK_OCID, display_name="my-a1-stack", lifecycle_state="ACTIVE")
JOB = JobInfo(id="ocid1.ormjob.oc1..job", operation="APPLY", lifecycle_state="SUCCEEDED")


def test_notifications_disabled_do_nothing(monkeypatch, capsys):
    monkeypatch.setattr(subprocess, "run", pytest.fail)
    monkeypatch.setattr(subprocess, "Popen", pytest.fail)
    notify.notify_success(Settings(notify_bell=False, notify_desktop=False), STACK, JOB)
    assert capsys.readouterr().out == ""


class FakeStdout(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_bell_rings_in_a_terminal(monkeypatch):
    fake = FakeStdout(tty=True)
    monkeypatch.setattr(notify.sys, "stdout", fake)
    notify.notify_success(Settings(notify_desktop=False), STACK, JOB)
    assert fake.getvalue() == "\a"


def test_bell_is_skipped_when_output_is_not_a_terminal(monkeypatch):
    fake = FakeStdout(tty=False)  # e.g. systemd journal or a redirected log
    monkeypatch.setattr(notify.sys, "stdout", fake)
    notify.notify_success(Settings(notify_desktop=False), STACK, JOB)
    assert fake.getvalue() == ""


def test_linux_desktop_notification_uses_notify_send(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(notify.subprocess, "run", lambda args, **kw: calls.append(args))
    notify.notify_success(Settings(notify_bell=False), STACK, JOB)
    assert calls[0][0] == "notify-send"
    assert "my-a1-stack" in calls[0][2]


def test_windows_desktop_notification_passes_text_via_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(notify.sys, "platform", "win32")
    monkeypatch.setattr(notify.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    notify.notify_success(Settings(notify_bell=False), STACK, JOB)
    args, kwargs = calls[0]
    assert args[0] == "powershell.exe"
    assert "my-a1-stack" not in " ".join(args)  # no string interpolation into the script
    assert "my-a1-stack" in kwargs["env"]["OCI_A1_NOTIFY_MESSAGE"]


def test_desktop_notification_failure_is_only_a_warning(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise OSError("no display")

    monkeypatch.setattr(notify.sys, "platform", "linux")
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/usr/bin/notify-send")
    monkeypatch.setattr(notify.subprocess, "run", boom)
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        notify.notify_success(Settings(notify_bell=False), STACK, JOB)
    assert "Desktop notification failed" in caplog.text


def test_success_command_gets_job_details(tmp_path):
    marker = tmp_path / "marker.txt"
    script = tmp_path / "hook.py"
    script.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(os.environ['OCI_A1_JOB_ID'])\n",
        encoding="utf-8",
    )
    command = f'"{sys.executable}" "{script}" "{marker}"'
    notify.notify_success(
        Settings(notify_bell=False, notify_desktop=False, success_command=command), STACK, JOB
    )
    assert marker.read_text(encoding="utf-8") == JOB.id


def test_failing_success_command_is_only_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        notify.notify_success(
            Settings(notify_bell=False, notify_desktop=False, success_command="exit 3"),
            STACK,
            JOB,
        )
    assert "exited with status 3" in caplog.text
