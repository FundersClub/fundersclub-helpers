import io
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from fchelper import configurator


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    def reset():
        configurator.configurations.clear()
        configurator.sources.clear()
        configurator.secure_keys.clear()
        configurator.pointed_keys.clear()
        configurator._loaded_prefix = None

    reset()
    for var in ("FCHELPER_OFFLINE", "FCHELPER_LOCAL_SECRETS", "TESTING", "QUIET", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    # Never read the real ~/.config/fundersclub from a test.
    monkeypatch.setenv("FCHELPER_POINTERS_FILE", str(tmp_path / "pointers.toml"))
    yield
    reset()


@pytest.fixture
def not_testing(monkeypatch):
    # The suite itself runs under pytest, which load() treats as a test run and
    # so skips pointers; tests of pointer behaviour pretend otherwise.
    monkeypatch.setattr(configurator, "_running_tests", lambda: False)


@pytest.fixture
def pointers(tmp_path):
    def write(body: str):
        (tmp_path / "pointers.toml").write_text(body)

    return write


def _client(developer_params=(), parameters=None):
    """An SSM client whose developer tier holds developer_params and whose single-parameter reads hit `parameters`."""
    client = MagicMock()
    client.get_parameters_by_path.return_value = {"Parameters": list(developer_params)}

    def get_parameter(Name, WithDecryption):
        assert WithDecryption is True
        if Name not in (parameters or {}):
            raise Exception(f"ParameterNotFound: {Name}")
        return {"Parameter": parameters[Name]}

    client.get_parameter.side_effect = get_parameter
    return client


def test_developer_load_reads_developer_tier(not_testing):
    client = _client(
        [
            {"Name": "/fc_developer/PARTNER_API_SECRET", "Type": "SecureString", "Value": "dev-secret"},
            {"Name": "/fc_developer/SANDBOX_ENABLED", "Type": "String", "Value": "true"},
        ]
    )
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        result = configurator.load("developer", "fc_")

    assert result == {"PARTNER_API_SECRET": "dev-secret", "SANDBOX_ENABLED": "true"}
    assert client.get_parameters_by_path.call_args.kwargs["Path"] == "/fc_developer/"
    assert configurator.sources["PARTNER_API_SECRET"] == "/fc_developer/PARTNER_API_SECRET"
    assert configurator.secure_keys == {"PARTNER_API_SECRET"}


def test_developer_load_without_aws_session_warns_and_continues(capsys):
    client = MagicMock()
    client.get_parameters_by_path.side_effect = Exception("Unable to locate credentials AKIAABCDEFGHIJKLMNOP")

    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        result = configurator.load("developer", "fc_")

    assert result == {}
    assert configurator.sources == {}
    assert "AKIAABCDEFGHIJKLMNOP" not in capsys.readouterr().out


def test_developer_load_offline_skips_aws(monkeypatch):
    monkeypatch.setenv("FCHELPER_OFFLINE", "true")
    with patch("fchelper.configurator._developer_ssm_client") as make_client:
        assert configurator.load("developer", "fc_") == {}
    make_client.assert_not_called()


def test_pointers_are_ignored_during_test_runs(pointers):
    # No not_testing fixture: under pytest, a staging pointer must not be resolved.
    pointers('[pointers]\nPARTNER_API_SECRET = "staging"\n')
    client = _client()
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")

    client.get_parameter.assert_not_called()
    assert "PARTNER_API_SECRET" not in configurator.configurations


def test_running_tests_detects_testing_env_var(monkeypatch):
    monkeypatch.setenv("TESTING", "true")
    assert configurator._running_tests()


def test_tier_pointer_overrides_developer_value_and_is_announced(not_testing, pointers, capsys):
    pointers('[pointers]\nPARTNER_API_SECRET = "staging"\n')
    client = _client(
        [{"Name": "/fc_developer/PARTNER_API_SECRET", "Type": "SecureString", "Value": "dev-secret"}],
        {"/fc_staging/PARTNER_API_SECRET": {"Type": "SecureString", "Value": "staging-secret"}},
    )
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")

    assert configurator.setting("PARTNER_API_SECRET") == "staging-secret"
    assert configurator.sources["PARTNER_API_SECRET"] == "/fc_staging/PARTNER_API_SECRET"
    assert configurator.non_developer_sources() == [
        {"key": "PARTNER_API_SECRET", "source": "/fc_staging/PARTNER_API_SECRET", "shadowed_by_env": False}
    ]
    err = capsys.readouterr().err
    assert err
    assert "staging-secret" not in err


def test_banner_is_shown_even_when_quiet(not_testing, pointers, monkeypatch, capsys):
    monkeypatch.setenv("QUIET", "true")
    pointers('[pointers]\nKEY = "production"\n')
    client = _client(parameters={"/fc_production/KEY": {"Type": "SecureString", "Value": "v"}})
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")

    assert capsys.readouterr().err


def test_full_ssm_path_pointer(not_testing, pointers):
    pointers('[pointers]\nCHAT_BOT_TOKEN = "ssm:/fc_other_staging/CHAT_TOKEN"\n')
    client = _client(parameters={"/fc_other_staging/CHAT_TOKEN": {"Type": "SecureString", "Value": "bot-token"}})
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")

    assert configurator.configurations["CHAT_BOT_TOKEN"] == "bot-token"
    assert configurator.sources["CHAT_BOT_TOKEN"] == "/fc_other_staging/CHAT_TOKEN"


def test_stringlist_pointer_is_split(not_testing, pointers):
    pointers('[pointers]\nALLOWED = "staging"\n')
    client = _client(parameters={"/fc_staging/ALLOWED": {"Type": "StringList", "Value": "a,b"}})
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")

    assert configurator.configurations["ALLOWED"] == ["a", "b"]
    assert "ALLOWED" not in configurator.secure_keys


def test_developer_tier_pointer_is_not_announced(not_testing, pointers, capsys):
    pointers('[pointers]\nKEY = "developer"\n')
    client = _client(parameters={"/fc_developer/KEY": {"Type": "SecureString", "Value": "v"}})
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")

    assert configurator.configurations["KEY"] == "v"
    assert configurator.non_developer_sources() == []
    assert capsys.readouterr().err == ""


def test_default_pointer_drops_the_developer_value(not_testing, pointers):
    pointers('[pointers]\nBASE_URL = "default"\n')
    client = _client([{"Name": "/fc_developer/BASE_URL", "Type": "String", "Value": "https://sandbox.example"}])
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")

    assert "BASE_URL" not in configurator.configurations
    assert configurator.setting("BASE_URL", "https://app-default.example") == "https://app-default.example"
    assert configurator.non_developer_sources() == [
        {"key": "BASE_URL", "source": configurator.DEFAULT_SOURCE, "shadowed_by_env": False}
    ]
    client.get_parameter.assert_not_called()


def test_1password_pointer_reads_with_op(not_testing, pointers):
    pointers('[pointers]\nPORTAL_PASSWORD = "op://Private/portal/password"\n')
    completed = MagicMock(returncode=0, stdout="hunter2", stderr="")
    with (
        patch("fchelper.configurator._developer_ssm_client", return_value=_client()),
        patch("fchelper.configurator.shutil.which", return_value="/usr/local/bin/op"),
        patch("fchelper.configurator.subprocess.run", return_value=completed) as run,
    ):
        configurator.load("developer", "fc_")

    assert run.call_args.args[0] == ["/usr/local/bin/op", "read", "--no-newline", "op://Private/portal/password"]
    assert configurator.configurations["PORTAL_PASSWORD"] == "hunter2"
    assert "PORTAL_PASSWORD" in configurator.secure_keys


def test_1password_pointer_without_op_installed_fails(not_testing, pointers):
    pointers('[pointers]\nX = "op://Private/x/password"\n')
    with (
        patch("fchelper.configurator._developer_ssm_client", return_value=_client()),
        patch("fchelper.configurator.shutil.which", return_value=None),
        pytest.raises(configurator.SecretPointerError) as excinfo,
    ):
        configurator.load("developer", "fc_")

    assert excinfo.value.reason == configurator.SecretPointerError.UNRESOLVABLE
    assert excinfo.value.key == "X"


def test_unresolvable_pointer_fails_loudly(not_testing, pointers, capsys):
    pointers('[pointers]\nPARTNER_API_SECRET = "production"\n')
    client = _client()  # AccessDenied/NotFound: nothing resolvable
    with (
        patch("fchelper.configurator._developer_ssm_client", return_value=client),
        pytest.raises(configurator.SecretPointerError) as excinfo,
    ):
        configurator.load("developer", "fc_")

    assert excinfo.value.reason == configurator.SecretPointerError.UNRESOLVABLE
    assert excinfo.value.key == "PARTNER_API_SECRET"
    # Printed too, since operations/payments swallow load() errors in development.
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    "body",
    [
        '[pointers]\nPARTNER_API_SECRET = "sk_live_not_a_pointer"\n',
        '[pointers]\nPARTNER_API_SECRET = "Staging"\n',
        "[pointers]\nPARTNER_API_SECRET = 1234\n",
    ],
)
def test_literal_values_are_rejected_without_being_echoed(not_testing, pointers, body):
    pointers(body)
    with (
        patch("fchelper.configurator._developer_ssm_client", return_value=_client()),
        pytest.raises(configurator.SecretPointerError) as excinfo,
    ):
        configurator.load("developer", "fc_")

    assert excinfo.value.reason == configurator.SecretPointerError.NOT_A_POINTER
    assert excinfo.value.key == "PARTNER_API_SECRET"
    assert "sk_live_not_a_pointer" not in str(excinfo.value)
    assert "1234" not in str(excinfo.value)


def test_keys_outside_the_pointers_table_are_rejected(not_testing, pointers):
    pointers('PARTNER_API_SECRET = "sk_live_x"\n[pointers]\n')
    with (
        patch("fchelper.configurator._developer_ssm_client", return_value=_client()),
        pytest.raises(configurator.SecretPointerError) as excinfo,
    ):
        configurator.load("developer", "fc_")
    assert excinfo.value.reason == configurator.SecretPointerError.UNEXPECTED_KEYS
    assert "sk_live_x" not in str(excinfo.value)


def test_invalid_toml_is_reported(not_testing, pointers):
    pointers("[pointers\n")
    with (
        patch("fchelper.configurator._developer_ssm_client", return_value=_client()),
        pytest.raises(configurator.SecretPointerError) as excinfo,
    ):
        configurator.load("developer", "fc_")
    assert excinfo.value.reason == configurator.SecretPointerError.INVALID_TOML


def test_environment_variable_still_wins_and_banner_says_so(not_testing, pointers, monkeypatch):
    monkeypatch.setenv("KEY", "from-env")
    pointers('[pointers]\nKEY = "staging"\n')
    client = _client(parameters={"/fc_staging/KEY": {"Type": "SecureString", "Value": "from-staging"}})
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")

    assert configurator.setting("KEY") == "from-env"
    assert configurator.non_developer_sources()[0]["shadowed_by_env"] is True


def test_no_pointers_file_means_no_single_parameter_reads(not_testing):
    client = _client()
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")
    client.get_parameter.assert_not_called()


def test_pointers_only_apply_to_the_developer_environment(not_testing, pointers):
    pointers('[pointers]\nKEY = "production"\n')
    client = _client()
    with patch("fchelper.configurator.boto3.client", return_value=client):
        configurator.load("staging", "fc_")
    client.get_parameter.assert_not_called()


def test_default_pointers_file_is_per_app_under_home(monkeypatch):
    monkeypatch.delenv("FCHELPER_POINTERS_FILE")
    path = configurator.pointers_file("fc_payments_")
    assert path == os.path.join(os.path.expanduser("~"), ".config", "fundersclub", "fc_payments.toml")


def test_deployed_load_records_sources_and_secure_keys():
    client = _client([{"Name": "/fc_staging/DB_PASSWORD", "Type": "SecureString", "Value": "x"}])
    with patch("fchelper.configurator.boto3.client", return_value=client):
        configurator.load("staging", "fc_")
    assert configurator.sources == {"DB_PASSWORD": "/fc_staging/DB_PASSWORD"}
    assert configurator.secure_keys == {"DB_PASSWORD"}


def _module(**values):
    module = types.ModuleType("thefundersclub.local_settings")
    for name, value in values.items():
        setattr(module, name, value)
    return module


def test_check_local_settings_flags_secret_names_and_secure_parameters():
    configurator.secure_keys.add("DATABASE_URL")
    module = _module(
        OPENAI_API_KEY="sk-abc",
        SERVICE_PASSWORD="pw",
        GITHUB_TOKEN="ghp",
        DATABASE_URL="postgres://u:p@h/db",
        SANDBOX_ENABLED="true",
        DEBUG_TOOLBAR_CONFIG={"SHOW": True},
        EMPTY_SECRET="",
        _PRIVATE_SECRET="x",
        lowercase_secret="x",
        DJANGO_SECRET_KEY="dev-only",
    )

    with pytest.raises(configurator.LocalSecretsError) as excinfo:
        configurator.check_local_settings(module, allow=("DJANGO_SECRET_KEY",), strict=True)

    assert set(excinfo.value.findings) == {"OPENAI_API_KEY", "SERVICE_PASSWORD", "GITHUB_TOKEN", "DATABASE_URL"}
    message = str(excinfo.value)
    for value in ("sk-abc", "pw", "ghp", "postgres://"):
        assert value not in message


def test_check_local_settings_clean_module_passes():
    assert configurator.check_local_settings(_module(SANDBOX_ENABLED="true", STATIC_URL="/static/")) == {}


def test_baseline_skips_values_copied_from_settings():
    # A local_settings that copies every settings global into itself: only what it changed or added counts.
    baseline = {"SETTINGS_SECRET": "from-settings", "CHANGED_TOKEN": "from-settings"}
    module = _module(SETTINGS_SECRET="from-settings", CHANGED_TOKEN="local", ADDED_PASSWORD="local")

    findings = configurator.check_local_settings(module, baseline=baseline)

    assert set(findings) == {"CHANGED_TOKEN", "ADDED_PASSWORD"}


def test_check_local_settings_warns_by_default(capsys):
    assert configurator.check_local_settings(_module(JWT_SECRET="x")) == {"JWT_SECRET": configurator.NOT_LOADED}
    assert capsys.readouterr().err


def test_env_can_downgrade_an_enforcing_repo_to_warn(monkeypatch, capsys):
    monkeypatch.setenv("FCHELPER_LOCAL_SECRETS", "warn")
    assert set(configurator.check_local_settings(_module(JWT_SECRET="x"), strict=True)) == {"JWT_SECRET"}
    assert capsys.readouterr().err


def test_env_can_opt_into_enforce_early(monkeypatch):
    monkeypatch.setenv("FCHELPER_LOCAL_SECRETS", "enforce")
    with pytest.raises(configurator.LocalSecretsError) as excinfo:
        configurator.check_local_settings(_module(JWT_SECRET="x"))
    assert set(excinfo.value.findings) == {"JWT_SECRET"}


def test_unknown_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("FCHELPER_LOCAL_SECRETS", "true")
    with pytest.raises(ValueError):
        configurator.check_local_settings(_module(JWT_SECRET="x"))


def test_migration_actions_for_each_secret(not_testing, pointers, capsys):
    pointers('[pointers]\nPOINTED_TOKEN = "staging"\n')
    client = _client(
        [
            {"Name": "/fc_developer/SAME_SECRET", "Type": "SecureString", "Value": "dev-value"},
            {"Name": "/fc_developer/OTHER_SECRET", "Type": "SecureString", "Value": "dev-value"},
        ],
        {"/fc_staging/POINTED_TOKEN": {"Type": "SecureString", "Value": "staging-value"}},
    )
    with patch("fchelper.configurator._developer_ssm_client", return_value=client):
        configurator.load("developer", "fc_")
    capsys.readouterr()

    module = _module(
        SAME_SECRET="dev-value",
        OTHER_SECRET="personal-value",
        POINTED_TOKEN="local-value",
        MISSING_SECRET="local-only",
    )
    findings = configurator.check_local_settings(module)

    assert findings == {
        "SAME_SECRET": configurator.MATCHES_DEVELOPER_TIER,
        "OTHER_SECRET": configurator.DIFFERS_FROM_DEVELOPER_TIER,
        "POINTED_TOKEN": configurator.OVERRIDES_POINTER,
        "MISSING_SECRET": configurator.MISSING_FROM_DEVELOPER_TIER,
    }
    err = capsys.readouterr().err
    for value in ("dev-value", "personal-value", "staging-value", "local-value", "local-only"):
        assert value not in err


def test_developer_client_skips_instance_metadata():
    with patch("fchelper.configurator.boto3.Session") as make_session:
        configurator._developer_ssm_client()

    botocore_session = make_session.call_args.kwargs["botocore_session"]
    methods = [provider.METHOD for provider in botocore_session.get_component("credential_provider").providers]
    assert "iam-role" not in methods
    assert "sso" in methods and "env" in methods


class _Terminal(io.StringIO):
    def isatty(self):
        return True


def test_warnings_are_bold_red_on_a_terminal(monkeypatch):
    terminal = _Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    configurator.check_local_settings(_module(JWT_SECRET="x"))

    lines = terminal.getvalue().splitlines()
    assert lines
    assert all(line.startswith("\033[1;31m") and line.endswith("\033[0m") for line in lines)


def test_no_color_disables_colour(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    terminal = _Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    configurator.check_local_settings(_module(JWT_SECRET="x"))

    assert terminal.getvalue()
    assert "\033[" not in terminal.getvalue()


def test_warnings_are_plain_when_not_a_terminal(capsys):
    configurator.check_local_settings(_module(JWT_SECRET="x"))
    err = capsys.readouterr().err
    assert err
    assert "\033[" not in err
