"""SDK wrapper: error classification, client construction, and safety surface."""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import oci
import pytest
from fakes import STACK_OCID, service_error, throttled

from oci_a1_retry.config import Settings
from oci_a1_retry.oci_client import (
    ErrorKind,
    OciCallError,
    OciSetupError,
    ResourceManagerGateway,
    build_gateway,
    classify,
    describe_error,
    load_oci_config,
    oci_config_location,
)

SOURCE_DIR = Path(__file__).resolve().parents[1] / "src" / "oci_a1_retry"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (429, ErrorKind.THROTTLED),
        (409, ErrorKind.CONFLICT),
        (401, ErrorKind.AUTH),
        (404, ErrorKind.NOT_FOUND),
        (400, ErrorKind.PERMANENT),
        (403, ErrorKind.PERMANENT),
        (408, ErrorKind.TRANSIENT),
        (500, ErrorKind.TRANSIENT),
        (502, ErrorKind.TRANSIENT),
        (503, ErrorKind.TRANSIENT),
        (504, ErrorKind.TRANSIENT),
    ],
)
def test_service_errors_are_classified_by_status(status, kind):
    assert classify(service_error(status, "Code", "msg")).kind is kind


def test_real_resource_manager_429_is_throttling_not_capacity():
    err = classify(throttled())
    assert err.kind is ErrorKind.THROTTLED
    assert err.status == 429
    assert err.code == "TooManyRequests"
    assert err.operation == "create_job"
    assert err.request_id == "test-request-id"


@pytest.mark.parametrize(
    "exc",
    [
        oci.exceptions.RequestException("connection reset"),
        oci.exceptions.ConnectTimeout("connect timed out"),
        ConnectionError("refused"),
        TimeoutError("read timed out"),
        OSError("[SSL] record layer failure"),
    ],
)
def test_network_errors_are_transient(exc):
    assert classify(exc).kind is ErrorKind.TRANSIENT


def test_unrelated_exceptions_are_not_swallowed():
    assert classify(ValueError("bug")) is None

    class Client:
        def get_job(self, job_id):
            raise ValueError("bug")

    with pytest.raises(ValueError):
        ResourceManagerGateway(Client()).get_job("x")


def test_gateway_converts_sdk_errors():
    class Client:
        def get_stack(self, stack_id):
            raise service_error(404, "NotAuthorizedOrNotFound", "nope", "get_stack")

    with pytest.raises(OciCallError) as excinfo:
        ResourceManagerGateway(Client()).get_stack(STACK_OCID)
    assert excinfo.value.kind is ErrorKind.NOT_FOUND


def test_remote_text_is_stripped_of_terminal_escapes():
    class Client:
        def get_stack(self, stack_id):
            return mock.Mock(
                data=mock.Mock(
                    id=STACK_OCID,
                    display_name="evil\x1b]0;pwned\x07\x1b[2Jname",
                    lifecycle_state="ACTIVE",
                    compartment_id="c",
                )
            )

        def get_job(self, job_id):
            raise service_error(400, "Bad", "msg\x1b[31m red\x9b", "get_job")

    gateway = ResourceManagerGateway(Client())
    assert gateway.get_stack(STACK_OCID).display_name == "evilname"
    with pytest.raises(OciCallError) as excinfo:
        gateway.get_job("x")
    assert str(excinfo.value) == "HTTP 400 Bad: msg red"


def test_job_log_bytes_are_decoded():
    class Client:
        def get_job_logs_content(self, job_id):
            return mock.Mock(data=b"Out of host capacity \xff")

    assert ResourceManagerGateway(Client()).get_job_log("x").startswith("Out of host capacity")


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_every_error_kind_has_a_description(kind):
    text = describe_error(OciCallError(kind, "HTTP 999 X: y"), Settings(region="eu-frankfurt-1"))
    assert "HTTP 999" in text


def test_build_gateway_disables_sdk_retries_and_circuit_breaker(oci_config):
    gateway = build_gateway(Settings(stack_ocid=STACK_OCID, oci_config_file=str(oci_config)))
    client = gateway._client
    assert gateway.region == "eu-frankfurt-1"
    assert isinstance(client.retry_strategy, oci.retry.NoneRetryStrategy)
    assert isinstance(
        client.base_client.circuit_breaker_strategy, oci.circuit_breaker.NoCircuitBreakerStrategy
    )


def test_sdk_sends_create_job_exactly_once_on_429(oci_config):
    """Guards against stacked retry loops: one 429 must mean one HTTP request."""
    gateway = build_gateway(Settings(stack_ocid=STACK_OCID, oci_config_file=str(oci_config)))
    with (
        mock.patch.object(
            gateway._client.base_client, "call_api", side_effect=throttled()
        ) as call_api,
        pytest.raises(OciCallError) as excinfo,
    ):
        gateway.create_apply_job(STACK_OCID, retry_token="t", display_name="d")
    assert excinfo.value.kind is ErrorKind.THROTTLED
    assert call_api.call_count == 1
    assert call_api.call_args.kwargs["header_params"]["opc-retry-token"] == "t"


def test_oci_config_location_matches_sdk_lookup(tmp_path, monkeypatch, oci_config):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no ~/.oci/config there
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("OCI_CONFIG_FILE", str(oci_config))
    assert oci_config_location(Settings()) == str(oci_config)
    assert load_oci_config(Settings())["region"] == "eu-frankfurt-1"  # SDK agrees
    explicit = Settings(oci_config_file=str(tmp_path / "x"))
    assert oci_config_location(explicit) == str(tmp_path / "x")
    monkeypatch.delenv("OCI_CONFIG_FILE")
    assert oci_config_location(Settings()).endswith("config")


def test_region_setting_overrides_profile_region(oci_config):
    config = load_oci_config(Settings(oci_config_file=str(oci_config), region="eu-milan-1"))
    assert config["region"] == "eu-milan-1"


def test_missing_oci_config_file_explains_setup(tmp_path):
    with pytest.raises(OciSetupError, match="oci setup config"):
        load_oci_config(Settings(oci_config_file=str(tmp_path / "nope")))


def test_missing_profile_is_reported(oci_config):
    with pytest.raises(OciSetupError, match=r"Profile \[OTHER\] not found"):
        load_oci_config(Settings(oci_config_file=str(oci_config), profile="OTHER"))


def test_incomplete_profile_is_reported(tmp_path):
    config = tmp_path / "config"
    config.write_text("[DEFAULT]\nregion=eu-frankfurt-1\n", encoding="utf-8")
    with pytest.raises(OciSetupError, match=r"incomplete or malformed|invalid"):
        load_oci_config(Settings(oci_config_file=str(config)))


def test_gateway_exposes_no_destructive_operations():
    public = {name for name in vars(ResourceManagerGateway) if not name.startswith("_")}
    assert public == {
        "get_stack",
        "get_stack_tf_config",
        "list_jobs",
        "create_apply_job",
        "get_job",
        "get_job_log",
    }


def test_source_never_calls_destructive_or_stack_changing_apis():
    forbidden = re.compile(
        r"delete_|cancel_job|update_stack|create_stack|update_job|change_stack|"
        r"DestroyJobOperationDetails|terminate_instance|ImportTfState",
        re.IGNORECASE,
    )
    offenders = [
        f"{path.name}: {match.group(0)}"
        for path in SOURCE_DIR.glob("*.py")
        for match in forbidden.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
