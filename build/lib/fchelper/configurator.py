import os
import re
import shutil
import subprocess
import sys
import tomllib

import boto3
import botocore.session

# Populated by load(); setting()/required_setting() read from this.
configurations = {}

# The prefix of the most recent load(), so check_local_settings() can say
# which developer-tier path a setting belongs in.
_loaded_prefix = None

# Where each key in `configurations` came from: an SSM parameter path or an
# op:// reference. Never holds values -- it's for the startup banner and for
# answering "why does my dev server have this credential?".
sources = {}

# Keys whose SSM parameter was a SecureString. check_local_settings() treats
# these as secrets even when the name doesn't look like one.
secure_keys = set()

# Keys set from the developer's pointers file (rather than a whole SSM tier).
pointed_keys = set()

DEVELOPER_ENVIRONMENT = "developer"

# Tiers a pointer may name on its own ("staging" -> /{prefix}staging/{KEY}).
# Anything else has to be spelled out as ssm:/full/path or op://vault/item/field.
POINTER_TIERS = ("developer", "staging", "production")

# Patterns that might leak into a botocore error message (e.g. from a
# misconfigured IAM policy ARN) and shouldn't be printed or re-raised as-is.
_AWS_ACCESS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_HEX_SECRET_RE = re.compile(r"[0-9a-fA-F]{32,}")

# Setting names that hold a credential. Deliberately broad: a false positive
# costs one entry in check_local_settings(allow=...), a false negative is a
# secret sitting in a plaintext file.
_SECRET_NAME_RE = re.compile(r"SECRET|TOKEN|PASSWORD|PASSPHRASE|CREDENTIAL|CREDS|APIKEY|_KEY(_ID)?$")


class SecretPointerError(RuntimeError):
    """A pointer in the developer's pointers file is malformed or couldn't be resolved."""

    # Values of `reason`.
    INVALID_TOML = "invalid_toml"
    UNEXPECTED_KEYS = "unexpected_keys"
    NOT_A_POINTER = "not_a_pointer"
    UNRESOLVABLE = "unresolvable"

    def __init__(self, message: str, *, reason: str, key: str = None):
        super().__init__(message)
        self.reason = reason
        self.key = key


class LocalSecretsError(Exception):
    """local_settings.py assigns a literal value to a secret setting. `findings` maps each name to an action code."""

    def __init__(self, message: str, *, findings):
        super().__init__(message)
        self.findings = findings


# Action codes check_local_settings() assigns to each secret found in local_settings.
MATCHES_DEVELOPER_TIER = "matches_developer_tier"
DIFFERS_FROM_DEVELOPER_TIER = "differs_from_developer_tier"
MISSING_FROM_DEVELOPER_TIER = "missing_from_developer_tier"
OVERRIDES_POINTER = "overrides_pointer"
OVERRIDES_PARAMETER = "overrides_parameter"
NOT_LOADED = "not_loaded"

_ADVICE = {
    MATCHES_DEVELOPER_TIER: "same value as {source}: delete it",
    DIFFERS_FROM_DEVELOPER_TIER: "differs from {source}: delete it, or use a pointer if you need another tier's value",
    MISSING_FROM_DEVELOPER_TIER: "not in {developer_prefix} yet: add a dev credential there, or use a pointer",
    OVERRIDES_POINTER: "overrides your pointer to {source} (local_settings wins): delete it",
    OVERRIDES_PARAMETER: "overrides {source}: delete it",
    NOT_LOADED: "move it to Parameter Store or your pointers file",
}


def _sanitize(message: str) -> str:
    message = _AWS_ACCESS_KEY_RE.sub("[REDACTED]", message)
    message = _HEX_SECRET_RE.sub("[REDACTED]", message)
    return message


_BOLD_RED = "\033[1;31m"
_RESET = "\033[0m"


def _alert(text: str):
    """Print a warning to stderr, bold red when stderr is a terminal (NO_COLOR turns that off)."""
    if not os.getenv("NO_COLOR") and sys.stderr.isatty():
        # Per line, so the colour survives anything that prefixes or interleaves lines.
        text = "\n".join(f"{_BOLD_RED}{line}{_RESET}" for line in text.splitlines())
    print(text, file=sys.stderr)


def _note(message: str):
    if not os.getenv("QUIET"):
        print(f"fchelper: {message}")


def _ssm_client():
    return boto3.client("ssm", region_name="us-east-1")


def _developer_ssm_client():
    # Laptops have no EC2 instance metadata, and botocore's probe for it costs
    # ~2s of startup on every run (and every runserver reload) when there's no
    # AWS session. Developer credentials come from SSO/profiles, so skip it.
    session = botocore.session.get_session()
    session.get_component("credential_provider").remove("iam-role")
    return boto3.Session(botocore_session=session).client("ssm", region_name="us-east-1")


def _parameter_value(param):
    param_type = param.get("Type")
    if param_type == "StringList":
        return param.get("Value", "").split(",")
    return param.get("Value")


def _fetch_path(client, parameter_prefix: str):
    """Return {name: (value, is_secure)} for every parameter under parameter_prefix."""
    values = {}
    # Parameters come in pages; keep fetching until NextToken runs out.
    next_token = "__placeholder__"
    while next_token:
        kwargs = {"Path": parameter_prefix, "WithDecryption": True}
        if next_token != "__placeholder__":
            kwargs["NextToken"] = next_token
        resp = client.get_parameters_by_path(**kwargs)

        for param in resp.get("Parameters", []):
            if param.get("Type") not in ("String", "SecureString", "StringList"):
                continue
            name = param["Name"].removeprefix(parameter_prefix)
            values[name] = (_parameter_value(param), param.get("Type") == "SecureString")

        next_token = resp.get("NextToken")
    return values


def _store(values, parameter_prefix: str):
    for name, (value, is_secure) in values.items():
        configurations[name] = value
        sources[name] = f"{parameter_prefix}{name}"
        if is_secure:
            secure_keys.add(name)


def load(environment: str, prefix: str):
    """
    Fetch settings from AWS Parameter Store under /{prefix}{environment}/ and
    populate the module-level `configurations` dict that setting() reads from.
    Returns that dict. Raises (with any credential-shaped substrings scrubbed
    from the message) on failure -- callers decide whether to swallow this for
    non-deployed environments.

    The developer environment reads /{prefix}developer/ on a best-effort basis
    (no AWS session just means repo defaults, with a warning), then applies the
    developer's personal pointers file -- see apply_pointers(). The test
    environment never touches AWS.
    """
    global _loaded_prefix
    configurations.clear()
    sources.clear()
    secure_keys.clear()
    pointed_keys.clear()
    _loaded_prefix = prefix

    if environment == "test":
        return configurations

    if environment == DEVELOPER_ENVIRONMENT:
        _load_developer(prefix)
        return configurations

    parameter_prefix = f"/{prefix}{environment}/"

    try:
        _store(_fetch_path(_ssm_client(), parameter_prefix), parameter_prefix)
    except Exception as e:
        if not os.getenv("QUIET"):
            print(f"Error fetching parameters from AWS: {_sanitize(str(e))}")
        raise RuntimeError("Error fetching parameters from AWS Parameter Store") from None

    if not os.getenv("QUIET"):
        print(f"AWS Secrets loaded: {len(configurations)} parameter(s)")

    return configurations


def _load_developer(prefix: str):
    parameter_prefix = f"/{prefix}{DEVELOPER_ENVIRONMENT}/"
    if os.getenv("FCHELPER_OFFLINE") == "true":
        _note(f"FCHELPER_OFFLINE=true; not reading {parameter_prefix}, using repo defaults")
    else:
        try:
            values = _fetch_path(_developer_ssm_client(), parameter_prefix)
        except Exception as e:
            # A developer without a live AWS session can still run the app on
            # repo defaults; this tier only ever holds dev/sandbox credentials.
            _note(f"couldn't read {parameter_prefix} ({_sanitize(str(e))}); using repo defaults")
        else:
            _store(values, parameter_prefix)
            _note(f"developer settings loaded from {parameter_prefix}: {len(values)} parameter(s)")

    if _running_tests():
        # A pointer at staging or production must never reach a test run.
        return
    apply_pointers(prefix)


def _running_tests() -> bool:
    return os.getenv("TESTING") == "true" or "pytest" in sys.modules or sys.argv[1:2] == ["test"]


def pointers_file(prefix: str) -> str:
    """
    The developer's personal pointers file for an app, e.g. ~/.config/fundersclub/fc_payments.toml
    for prefix "fc_payments_". It lives outside every repo so it's shared by all worktrees and can't
    be committed. FCHELPER_POINTERS_FILE overrides the location.
    """
    override = os.getenv("FCHELPER_POINTERS_FILE")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".config", "fundersclub", f"{prefix.rstrip('_')}.toml")


def read_pointers(path: str):
    """
    Parse a pointers file into {KEY: pointer}. The file holds only pointers:

        [pointers]
        PARTNER_API_SECRET = "staging"                       # /{prefix}staging/PARTNER_API_SECRET
        CHAT_BOT_TOKEN = "ssm:/fc_staging/CHAT_BOT_TOKEN"
        PORTAL_PASSWORD = "op://Private/portal/password"

    Anything that isn't a pointer is rejected, and error messages name the key
    but never echo the value, since a rejected value may be a pasted secret.
    """
    if not os.path.exists(path):
        return {}

    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SecretPointerError(f"{path} isn't valid TOML: {e}", reason=SecretPointerError.INVALID_TOML) from None

    stray = sorted(key for key in data if key != "pointers")
    if stray:
        raise SecretPointerError(
            f"{path}: only a [pointers] table is allowed; found {', '.join(stray)}",
            reason=SecretPointerError.UNEXPECTED_KEYS,
        )

    pointers = data.get("pointers", {})
    for key, pointer in pointers.items():
        if not _is_pointer(pointer):
            raise SecretPointerError(
                f"{path}: {key} isn't a pointer. Use one of {', '.join(POINTER_TIERS)}, "
                "ssm:/full/parameter/path or op://vault/item/field -- never a literal value.",
                reason=SecretPointerError.NOT_A_POINTER,
                key=key,
            )
    return pointers


def _is_pointer(pointer) -> bool:
    if not isinstance(pointer, str):
        return False
    return pointer in POINTER_TIERS or pointer.startswith("ssm:/") or pointer.startswith("op://")


def _read_1password(reference: str) -> str:
    op = shutil.which("op")
    if not op:
        raise RuntimeError("the 1Password CLI (op) isn't installed")
    # Generous timeout: op may be waiting on a Touch ID prompt.
    result = subprocess.run([op, "read", "--no-newline", reference], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"op read failed: {result.stderr.strip()}")
    return result.stdout


def apply_pointers(prefix: str):
    """
    Resolve the developer's pointers file into `configurations`, then print a
    banner naming every key that came from somewhere other than the developer
    tier. Values only ever exist in this process; nothing is written to disk.

    SSM pointers are read with whatever AWS session is active, so IAM decides
    who can point at staging or production. Any pointer that can't be resolved
    raises SecretPointerError: silently falling back to a dev value would leave
    the developer testing against something other than what they asked for.
    """
    path = pointers_file(prefix)
    pointers = read_pointers(path)
    if not pointers:
        return

    client = None
    for key, pointer in pointers.items():
        try:
            if pointer.startswith("op://"):
                value, source, is_secure = _read_1password(pointer), pointer, True
            else:
                source = pointer.removeprefix("ssm:") if pointer.startswith("ssm:") else f"/{prefix}{pointer}/{key}"
                client = client or _developer_ssm_client()
                param = client.get_parameter(Name=source, WithDecryption=True)["Parameter"]
                value, is_secure = _parameter_value(param), param.get("Type") == "SecureString"
        except Exception as e:
            message = f"couldn't resolve {key} -> {pointer} from {path}: {_sanitize(str(e))}"
            # Printed as well as raised: some callers swallow load() errors in development.
            _alert(f"fchelper: ✗ {message}")
            raise SecretPointerError(message, reason=SecretPointerError.UNRESOLVABLE, key=key) from None

        configurations[key] = value
        sources[key] = source
        pointed_keys.add(key)
        if is_secure:
            secure_keys.add(key)
        else:
            secure_keys.discard(key)

    _print_banner(path)


def non_developer_sources():
    """
    The pointed settings that came from outside the developer tier, as
    [{"key", "source", "shadowed_by_env"}]. Names and paths only, never values.
    """
    developer_prefix = f"/{_loaded_prefix}{DEVELOPER_ENVIRONMENT}/"
    return [
        {"key": key, "source": sources[key], "shadowed_by_env": bool(os.getenv(key))}
        for key in sorted(pointed_keys)
        if not sources[key].startswith(developer_prefix)
    ]


def _print_banner(path: str):
    entries = non_developer_sources()
    if not entries:
        return
    # Always shown, even with QUIET: this is the reminder that a local process
    # is holding staging or production credentials.
    lines = [f"fchelper: ⚠ settings NOT from the developer tier (pointers in {path}):"]
    width = max(len(entry["key"]) for entry in entries)
    for entry in entries:
        shadowed = "  (shadowed by an environment variable)" if entry["shadowed_by_env"] else ""
        lines.append(f"fchelper:     {entry['key'].ljust(width)}  <- {entry['source']}{shadowed}")
    _alert("\n".join(lines))


def check_local_settings(module, *, allow=(), strict=False, baseline=None):
    """
    Report literal secrets in a developer's local_settings module, with what to
    do about each one. Call it right after `from .local_settings import *`,
    passing the module itself.

    `baseline` is the importing settings module's globals from just before
    that import. Pass it when local_settings copies those globals into its own
    namespace: only names local_settings added or changed are checked.

    A setting is flagged when local_settings assigns it a non-empty string and
    either its name looks like a credential or it came from a SecureString in
    Parameter Store. Secrets belong in the developer tier or the pointers file;
    local_settings.py is for non-secret tweaks.

    Warns by default so teams can migrate at their own pace; strict=True makes
    it raise LocalSecretsError instead. FCHELPER_LOCAL_SECRETS=warn|enforce
    overrides `strict` for one developer (to opt in early, or to run while
    mid-migration once a repo enforces). `allow` lists names that are safe to
    set locally (e.g. a dev-only SECRET_KEY). Messages name settings, never
    values. Returns {name: action code} for every flagged setting (see the
    MATCHES_DEVELOPER_TIER etc. constants).
    """
    mode = os.getenv("FCHELPER_LOCAL_SECRETS") or ("enforce" if strict else "warn")
    if mode not in ("warn", "enforce"):
        raise ValueError("FCHELPER_LOCAL_SECRETS must be 'warn' or 'enforce'")

    findings = {}
    for name, value in vars(module).items():
        if name.startswith("_") or not name.isupper() or name in allow:
            continue
        if not isinstance(value, (str, bytes)) or not value:
            continue
        if baseline is not None and name in baseline and baseline[name] == value:
            continue
        if name in secure_keys or _SECRET_NAME_RE.search(name):
            findings[name] = _migration_action(name, value)

    if not findings:
        return findings

    developer_prefix = f"/{_loaded_prefix}{DEVELOPER_ENVIRONMENT}/"
    names = sorted(findings)
    width = max(len(name) for name in names)
    lines = [f"{module.__name__} sets {len(names)} secret setting(s) that belong in Parameter Store:"]
    for name in names:
        advice = _ADVICE[findings[name]].format(source=sources.get(name), developer_prefix=developer_prefix)
        lines.append(f"    {name.ljust(width)}  {advice}")
    lines.append("  Safe to keep locally (e.g. a dev-only key)? List it in check_local_settings(allow=...).")
    message = "\n".join(lines)

    if mode == "enforce":
        raise LocalSecretsError(
            f"{message}\n  To run anyway while you migrate: FCHELPER_LOCAL_SECRETS=warn", findings=findings
        )
    _alert("\n".join(f"fchelper: ⚠ {line}" for line in message.splitlines()))
    return findings


def _migration_action(name: str, value) -> str:
    """What to do with a secret found in local_settings, judged against what load() found."""
    if _loaded_prefix is None:
        return NOT_LOADED
    source = sources.get(name)
    if name in pointed_keys:
        return OVERRIDES_POINTER
    if source is None:
        return MISSING_FROM_DEVELOPER_TIER
    if source.startswith(f"/{_loaded_prefix}{DEVELOPER_ENVIRONMENT}/"):
        return MATCHES_DEVELOPER_TIER if value == configurations.get(name) else DIFFERS_FROM_DEVELOPER_TIER
    return OVERRIDES_PARAMETER


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
