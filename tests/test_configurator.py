from unittest.mock import MagicMock, patch

import pytest
from django.core.exceptions import ImproperlyConfigured

from fchelper import configurator


@pytest.fixture(autouse=True)
def _reset_configurations():
    # configurator.configurations is a shared module-level dict; isolate tests.
    configurator.configurations.clear()
    yield
    configurator.configurations.clear()


def _client_with_response(*pages):
    client = MagicMock()
    client.get_parameters_by_path.side_effect = pages
    return client


def test_load_developer_and_test_skip_aws_entirely():
    with patch("fchelper.configurator.boto3.client") as make_client:
        result = configurator.load("developer", "fc_payments_")
        result_test = configurator.load("test", "fc_payments_")

    assert result == {}
    assert result_test == {}
    make_client.assert_not_called()


def test_load_handles_string_securestring_stringlist_and_pagination():
    page1 = {
        "Parameters": [
            {"Name": "/fc_payments_staging/DJANGO_SECRET_KEY", "Type": "SecureString", "Value": "shh"},
            {"Name": "/fc_payments_staging/SOME_URL", "Type": "String", "Value": "https://example.com"},
        ],
        "NextToken": "page2",
    }
    page2 = {
        "Parameters": [
            {"Name": "/fc_payments_staging/ALLOWED_ORIGINS", "Type": "StringList", "Value": "a.com,b.com"},
        ],
    }
    client = _client_with_response(page1, page2)

    with patch("fchelper.configurator.boto3.client", return_value=client):
        result = configurator.load("staging", "fc_payments_")

    assert result == {
        "DJANGO_SECRET_KEY": "shh",
        "SOME_URL": "https://example.com",
        "ALLOWED_ORIGINS": ["a.com", "b.com"],
    }
    # setting() reads from the same populated dict load() returns.
    assert configurator.setting("DJANGO_SECRET_KEY") == "shh"

    # Every page must request decryption, or SecureString values come back empty/ciphertext.
    for call in client.get_parameters_by_path.call_args_list:
        assert call.kwargs["WithDecryption"] is True
    assert client.get_parameters_by_path.call_args_list[1].kwargs["NextToken"] == "page2"


def test_load_clears_stale_state_from_a_prior_call():
    client = _client_with_response({"Parameters": [{"Name": "/fc_x_staging/A", "Type": "String", "Value": "1"}]})
    with patch("fchelper.configurator.boto3.client", return_value=client):
        configurator.load("staging", "fc_x_")
    assert configurator.configurations == {"A": "1"}

    client2 = _client_with_response({"Parameters": [{"Name": "/fc_x_staging/B", "Type": "String", "Value": "2"}]})
    with patch("fchelper.configurator.boto3.client", return_value=client2):
        configurator.load("staging", "fc_x_")
    # "A" from the first call must not leak into the second.
    assert configurator.configurations == {"B": "2"}


def test_load_sanitizes_credentials_from_error_output(capsys):
    client = MagicMock()
    client.get_parameters_by_path.side_effect = Exception(
        "denied for AKIAABCDEFGHIJKLMNOP and token deadbeefdeadbeefdeadbeefdeadbeef"
    )

    with patch("fchelper.configurator.boto3.client", return_value=client), pytest.raises(RuntimeError):
        configurator.load("staging", "fc_payments_")

    printed = capsys.readouterr().out
    assert "AKIAABCDEFGHIJKLMNOP" not in printed
    assert "deadbeefdeadbeefdeadbeefdeadbeef" not in printed
    assert "[REDACTED]" in printed


def test_setting_precedence_env_over_ssm_over_default(monkeypatch):
    configurator.configurations["KEY"] = "from-ssm"
    monkeypatch.setenv("KEY", "from-env")
    assert configurator.setting("KEY", "from-default") == "from-env"

    monkeypatch.delenv("KEY")
    assert configurator.setting("KEY", "from-default") == "from-ssm"

    configurator.configurations.clear()
    assert configurator.setting("KEY", "from-default") == "from-default"


def test_required_setting_raises_when_deployed_and_unset():
    with pytest.raises(ImproperlyConfigured):
        configurator.required_setting("MISSING", is_deployed_env=True)


def test_required_setting_falls_back_to_default_when_not_deployed():
    assert configurator.required_setting("MISSING", "dev-default", is_deployed_env=False) == "dev-default"


def test_required_setting_returns_value_when_present():
    configurator.configurations["PRESENT"] = "real-value"
    assert configurator.required_setting("PRESENT", is_deployed_env=True) == "real-value"


@pytest.mark.parametrize(
    "value,expected", [("true", True), ("True", True), ("false", False), (True, True), (False, False)]
)
def test_convert_value_bool_handles_strings_and_real_bools(value, expected):
    assert configurator.convert_value(value, "bool") is expected


def test_convert_value_list_handles_none():
    assert configurator.convert_value(None, "list") == []


def test_convert_value_list_splits_and_strips_csv_string():
    assert configurator.convert_value("a, b ,c", "list") == ["a", "b", "c"]
