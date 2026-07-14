"""Helpers for distinguishing canonical deployments from ephemeral previews."""

import argparse
import os


def environment_name(environment=None):
    """Return the logical application environment (staging, production, etc.)."""
    return environment if environment is not None else os.getenv("ENVIRONMENT", "developer")


def deployment_name(deployment=None, *, environment=None):
    """Return the concrete deployment, defaulting legacy deployments to their environment."""
    if deployment is not None:
        return deployment
    return os.getenv("DEPLOYMENT") or environment_name(environment)


def is_canonical_deployment(*, environment=None, deployment=None):
    """Whether this process is the canonical deployment for its logical environment."""
    logical_environment = environment_name(environment)
    return deployment_name(deployment, environment=logical_environment) == logical_environment


def is_ephemeral_deployment(*, environment=None, deployment=None):
    """Whether this process is a PR preview running with staging configuration."""
    logical_environment = environment_name(environment)
    concrete_deployment = deployment_name(deployment, environment=logical_environment)
    return logical_environment == "staging" and concrete_deployment.startswith("pr-")


def has_ephemeral_database(value=None):
    """Whether the deployment has a database isolated from its canonical environment."""
    if value is None:
        value = os.getenv("DATABASE_IS_EPHEMERAL", "false")
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "yes")


def should_run_migrations(*, environment=None, deployment=None, database_is_ephemeral=None):
    """
    Run migrations on canonical deployments and on PR previews with an explicitly
    isolated database. Fail closed for non-canonical deployments sharing a database.
    """
    if is_canonical_deployment(environment=environment, deployment=deployment):
        return True
    return is_ephemeral_deployment(
        environment=environment,
        deployment=deployment,
    ) and has_ephemeral_database(database_is_ephemeral)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect FundersClub deployment runtime behavior.")
    parser.add_argument("command", choices=("should-migrate",))
    args = parser.parse_args(argv)

    if args.command == "should-migrate":
        should_migrate = should_run_migrations()
        print("true" if should_migrate else "false")
        return 0 if should_migrate else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
