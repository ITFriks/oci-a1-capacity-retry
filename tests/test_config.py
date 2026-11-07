"""Configuration loading, precedence, validation and per-platform paths."""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import STACK_OCID

from oci_a1_retry.config import ConfigError, Settings, load_settings, validate_settings
from oci_a1_retry.paths import APP_NAME, user_config_dir, user_state_dir


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def user_dirs(base: Path) -> dict[str, str]:
    """Point the per-user config dir at ``base`` on every OS (XDG on Linux, APPDATA on Windows)."""
    return {"XDG_CONFIG_HOME": str(base), "APPDATA": str(base)}


def load(tmp_path, env=None, cli=None, config_path=None):
    isolated = {**user_dirs(tmp_path / "user-config"), **(env or {})}
    return load_settings(cli, config_path=config_path, env=isolated, cwd=tmp_path)


def test_defaults_without_any_config(tmp_path):
    loaded = load(tmp_path)
    assert loaded.path is None
    assert loaded.settings == Settings()
    assert loaded.settings.capacity_retry_seconds == 300
    assert loaded.settings.job_poll_seconds == 15
    assert loaded.settings.throttle_initial_seconds == 5
    assert loaded.settings.throttle_max_seconds == 60
    assert loaded.settings.profile == "DEFAULT"


def test_precedence_cli_over_env_over_file_over_defaults(tmp_path):
    write(
        tmp_path / "config.toml",
        f'stack_ocid = "{STACK_OCID}"\nregion = "eu-milan-1"\nprofile = "FILE"\n'
        "job_poll_seconds = 20\n",
    )
    env = {"OCI_A1_REGION": "eu-paris-1", "OCI_A1_PROFILE": "ENV"}

    settings = load(tmp_path, env=env, cli={"profile": "CLI", "region": None}).settings

    assert settings.stack_ocid == STACK_OCID  # file
    assert settings.job_poll_seconds == 20  # file
    assert settings.region == "eu-paris-1"  # env beats file
    assert settings.profile == "CLI"  # cli beats env
    assert settings.capacity_retry_seconds == 300  # default


def test_environment_values_are_typed(tmp_path):
    env = {"OCI_A1_CAPACITY_RETRY_SECONDS": "600", "OCI_A1_NOTIFY_BELL": "no"}
    settings = load(tmp_path, env=env).settings
    assert settings.capacity_retry_seconds == 600
    assert settings.notify_bell is False


def test_explicit_config_path_and_env_config_path(tmp_path):
    other = write(tmp_path / "elsewhere.toml", 'profile = "X"\n')
    assert load(tmp_path, config_path=str(other)).settings.profile == "X"
    assert load(tmp_path, env={"OCI_A1_CONFIG": str(other)}).settings.profile == "X"


def test_user_config_dir_is_used_when_cwd_has_none(tmp_path):
    xdg = tmp_path / "xdg"
    (xdg / APP_NAME).mkdir(parents=True)
    write(xdg / APP_NAME / "config.toml", 'profile = "USERDIR"\n')
    cwd = tmp_path / "work"
    cwd.mkdir()
    loaded = load_settings(env=user_dirs(xdg), cwd=cwd)
    assert loaded.settings.profile == "USERDIR"


def test_missing_explicit_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path, config_path=str(tmp_path / "missing.toml"))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("stack_ocid = [", "not valid TOML"),
        ("capacity_retry_secs = 300\n", "unknown setting"),
        ('capacity_retry_seconds = "soon"\n', "must be an integer"),
        ("capacity_retry_seconds = 300.5\n", "must be an integer"),
        ("capacity_retry_seconds = true\n", "must be an integer"),
        ("notify_bell = 1\n", "must be true or false"),
        ("profile = 5\n", "must be a string"),
    ],
)
def test_invalid_config_files_are_rejected(tmp_path, content, message):
    write(tmp_path / "config.toml", content)
    with pytest.raises(ConfigError, match=message):
        load(tmp_path)


def test_invalid_environment_value_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="OCI_A1_JOB_POLL_SECONDS"):
        load(tmp_path, env={"OCI_A1_JOB_POLL_SECONDS": "often"})


def test_valid_settings_have_no_problems():
    assert validate_settings(Settings(stack_ocid=STACK_OCID, region="eu-frankfurt-1")) == []


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"stack_ocid": ""}, "stack_ocid is not set"),
        ({"stack_ocid": "ocid1.ormstack.oc1.eu-frankfurt-1.REPLACE_ME"}, "example placeholder"),
        ({"stack_ocid": "ocid1.ormstack.oc1.eu-frankfurt-1.Has Spaces"}, "not a valid"),
        ({"stack_ocid": "ocid1.ormjob.oc1.eu-frankfurt-1.aaaa"}, "'ormjob' OCID"),
        ({"region": "Frankfurt"}, "not a valid OCI region"),
        ({"profile": ""}, "profile must not be empty"),
        ({"capacity_retry_seconds": 5}, "capacity_retry_seconds must be between"),
        ({"job_poll_seconds": 1}, "job_poll_seconds must be between"),
        ({"throttle_initial_seconds": 30, "throttle_max_seconds": 10}, "throttle_max_seconds"),
    ],
)
def test_validation_problems(overrides, message):
    values = {"stack_ocid": STACK_OCID, **overrides}
    problems = validate_settings(Settings(**values))
    assert any(message in problem for problem in problems), problems


def test_example_config_is_rejected_until_stack_ocid_is_replaced(tmp_path):
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    settings = load(tmp_path, config_path=str(example)).settings
    problems = validate_settings(settings)
    assert len(problems) == 1
    assert "stack_ocid" in problems[0]


# -- Windows behaviour (pure path logic, runs on every OS) -------------------


def test_windows_paths_use_appdata_and_localappdata():
    env = {
        "APPDATA": r"C:\Users\me\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\me\AppData\Local",
    }
    config_dir = user_config_dir("win32", env)
    state_dir = user_state_dir("win32", env)
    assert config_dir.name == state_dir.name == APP_NAME
    assert str(config_dir.parent) == env["APPDATA"]
    assert str(state_dir.parent) == env["LOCALAPPDATA"]


def test_windows_paths_fall_back_to_home_when_env_missing():
    assert user_state_dir("win32", {}) == Path.home() / "AppData" / "Local" / APP_NAME
    assert user_config_dir("win32", {}) == Path.home() / "AppData" / "Roaming" / APP_NAME


def test_linux_paths_follow_xdg():
    assert user_state_dir("linux", {}) == Path.home() / ".local" / "state" / APP_NAME
    assert user_config_dir("linux", {"XDG_CONFIG_HOME": "/x"}) == Path("/x") / APP_NAME


def test_windows_path_in_single_quotes_is_read_verbatim(tmp_path):
    write(tmp_path / "config.toml", "log_dir = 'C:\\Users\\me\\oci logs'\n")
    assert load(tmp_path).settings.log_dir == r"C:\Users\me\oci logs"


def test_windows_path_in_double_quotes_gives_a_helpful_hint(tmp_path):
    write(tmp_path / "config.toml", 'oci_config_file = "C:\\Users\\me\\.oci\\config"\n')
    with pytest.raises(ConfigError, match="single quotes"):
        load(tmp_path)


def test_config_file_with_utf8_bom_and_crlf_line_endings(tmp_path):
    bom_crlf = b'\xef\xbb\xbfprofile = "WIN"\r\nregion = "eu-frankfurt-1"\r\n'
    (tmp_path / "config.toml").write_bytes(bom_crlf)
    assert load(tmp_path).settings.profile == "WIN"
