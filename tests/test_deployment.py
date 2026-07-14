import pytest

from fchelper.deployment import (
    deployment_name,
    has_ephemeral_database,
    is_canonical_deployment,
    is_ephemeral_deployment,
    should_run_migrations,
)


@pytest.mark.parametrize(
    "environment,deployment,expected",
    [
        ("staging", "staging", True),
        ("production", "production", True),
        ("developer", None, True),
        ("staging", "pr-123", False),
        ("production", "pr-123", False),
    ],
)
def test_is_canonical_deployment(environment, deployment, expected):
    assert is_canonical_deployment(environment=environment, deployment=deployment) is expected


def test_deployment_name_defaults_to_environment_for_legacy_deployments(monkeypatch):
    monkeypatch.delenv("DEPLOYMENT", raising=False)
    assert deployment_name(environment="staging") == "staging"


@pytest.mark.parametrize(
    "environment,deployment,expected",
    [
        ("staging", "pr-123", True),
        ("staging", "staging", False),
        ("production", "pr-123", False),
        ("staging", "feature-branch", False),
    ],
)
def test_is_ephemeral_deployment(environment, deployment, expected):
    assert is_ephemeral_deployment(environment=environment, deployment=deployment) is expected


@pytest.mark.parametrize("value", [True, "true", "True", "1", "yes"])
def test_has_ephemeral_database_accepts_explicit_true_values(value):
    assert has_ephemeral_database(value) is True


@pytest.mark.parametrize("value", [False, "false", "0", "", None])
def test_has_ephemeral_database_rejects_false_values(value, monkeypatch):
    monkeypatch.delenv("DATABASE_IS_EPHEMERAL", raising=False)
    assert has_ephemeral_database(value) is False


@pytest.mark.parametrize(
    "environment,deployment,database_is_ephemeral,expected",
    [
        ("staging", "staging", False, True),
        ("production", "production", False, True),
        ("staging", "pr-123", True, True),
        ("staging", "pr-123", False, False),
        ("staging", "feature-branch", True, False),
        ("production", "pr-123", True, False),
    ],
)
def test_should_run_migrations(environment, deployment, database_is_ephemeral, expected):
    assert (
        should_run_migrations(
            environment=environment,
            deployment=deployment,
            database_is_ephemeral=database_is_ephemeral,
        )
        is expected
    )


def test_cli_exit_code_reflects_migration_decision(monkeypatch):
    from fchelper.deployment import main

    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DEPLOYMENT", "pr-42")
    monkeypatch.setenv("DATABASE_IS_EPHEMERAL", "true")
    assert main(["should-migrate"]) == 0

    monkeypatch.setenv("DATABASE_IS_EPHEMERAL", "false")
    assert main(["should-migrate"]) == 1
