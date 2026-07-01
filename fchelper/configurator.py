import os
import re

import boto3

# Populated by load(); setting()/required_setting() read from this.
configurations = {}

# Patterns that might leak into a botocore error message (e.g. from a
# misconfigured IAM policy ARN) and shouldn't be printed or re-raised as-is.
_AWS_ACCESS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_HEX_SECRET_RE = re.compile(r"[0-9a-fA-F]{32,}")


def _sanitize(message: str) -> str:
    message = _AWS_ACCESS_KEY_RE.sub("[REDACTED]", message)
    message = _HEX_SECRET_RE.sub("[REDACTED]", message)
    return message


def load(environment: str, prefix: str):
    """
    Fetch settings from AWS Parameter Store under /{prefix}{environment}/ and
    populate the module-level `configurations` dict that setting() reads from.
    Returns that dict. Raises (with any credential-shaped substrings scrubbed
    from the message) on failure -- callers decide whether to swallow this for
    non-deployed environments.
    """
    configurations.clear()

    if environment in ("developer", "test"):
        # Developer/test just use the defaults baked into the calling app.
        return configurations

    parameter_prefix = f"/{prefix}{environment}/"
    client = boto3.client("ssm", region_name="us-east-1")

    try:
        # Parameters come in pages; keep fetching until NextToken runs out.
        next_token = "__placeholder__"
        while next_token:
            kwargs = {"Path": parameter_prefix, "WithDecryption": True}
            if next_token != "__placeholder__":
                kwargs["NextToken"] = next_token
            resp = client.get_parameters_by_path(**kwargs)

            for param in resp.get("Parameters", []):
                name = param["Name"].removeprefix(parameter_prefix)
                param_type = param.get("Type")
                if param_type == "String" or param_type == "SecureString":
                    configurations[name] = param.get("Value")
                elif param_type == "StringList":
                    configurations[name] = param.get("Value", "").split(",")

            next_token = resp.get("NextToken")

    except Exception as e:
        if not os.getenv("QUIET"):
            print(f"Error fetching parameters from AWS: {_sanitize(str(e))}")
        raise RuntimeError("Error fetching parameters from AWS Parameter Store") from None

    if not os.getenv("QUIET"):
        print(f"AWS Secrets loaded: {len(configurations)} parameter(s)")

    return configurations


def setting(key: str, default_value=None, data_type: str = "str"):
    """
    Return a setting from an environment variable, AWS Parameter Store (via the
    most recent load() call), or the default value if it's not set anywhere.
    """

    # Falls back based on falsy-ness, not just presence: an explicitly-empty
    # env var still falls through to Parameter Store / the default.
    value = os.getenv(key, None)
    value = value or configurations.get(key, None)
    value = value or default_value

    return convert_value(value, data_type)


def required_setting(key: str, default_value=None, data_type: str = "str", *, is_deployed_env: bool):
    """
    Like setting(), but raises ImproperlyConfigured if the resolved value is
    empty while is_deployed_env=True. Pass the calling app's own notion of
    "this is a deployed environment" (e.g. ENVIRONMENT in ("production", "staging"))
    -- this library has no opinion on what counts as deployed.
    """
    # Imported lazily so fchelper.configurator doesn't force a hard Django
    # dependency on consumers that only use setting()/load().
    from django.core.exceptions import ImproperlyConfigured

    default = None if is_deployed_env else default_value
    value = setting(key, default, data_type)
    if is_deployed_env and value in (None, "", []):
        raise ImproperlyConfigured(f"{key} must be configured for a deployed environment")
    return value


def convert_value(value, data_type: str):
    if data_type == "str":
        return value or ""

    if data_type == "int":
        return int(value)

    if data_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() == "true"

    if data_type == "list":
        if isinstance(value, str):
            value = value.split(",")
        return [item.strip() for item in (value or [])]

    return value
