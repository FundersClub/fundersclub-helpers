# fundersclub-helpers

Helpers for the Funder's Club applications. Usable as a Python package.

## Settings and secrets

`fchelper.configurator.setting(KEY)` resolves an environment variable, then
Parameter Store (loaded by `load(ENVIRONMENT, prefix)`), then the default.

- **Deployed environments** read `/{prefix}{environment}/` and fail closed.
- **`developer`** reads the shared `/{prefix}developer/` tier when an AWS
  session is available (otherwise repo defaults, with a warning;
  `FCHELPER_OFFLINE=true` skips it), then applies your personal pointers file.
- **`test`** never touches AWS.

Your pointers file lives at `~/.config/fundersclub/<prefix>.toml` (e.g.
`fc.toml` for prefix `fc_`), outside every repo, and holds only pointers:

```toml
[pointers]
PARTNER_API_SECRET = "staging"                     # /fc_staging/PARTNER_API_SECRET
CHAT_BOT_TOKEN = "ssm:/fc_staging/CHAT_BOT_TOKEN"  # any parameter path
PORTAL_PASSWORD = "op://Private/portal/password"   # 1Password, via `op read`
```

Values are fetched at startup with your current AWS session (or 1Password) and
exist only in the process. A banner names every key that came from outside the
developer tier; an unresolvable pointer raises `SecretPointerError`. Pointers
are ignored during test runs. Delete the line to go back to the shared default.

After `from .local_settings import *`, call
`check_local_settings(local_settings_module, allow=(...))`. It lists every
literal secret in `local_settings.py` with what to do about it (already in the
developer tier, missing from it, overriding a pointer, ...). It warns by
default; `strict=True` makes it an error. `FCHELPER_LOCAL_SECRETS=warn|enforce`
overrides that for one developer. `local_settings.py` is for non-secret tweaks
only.

## Deployment identity and migrations

Applications use `ENVIRONMENT` for their logical configuration namespace and
`DEPLOYMENT` for the concrete runtime (`staging`, `production`, or `pr-<n>`).
`fchelper.deployment` centralizes the distinction:

```python
from fchelper.deployment import is_canonical_deployment, should_run_migrations

is_canonical_deployment()
should_run_migrations()
```

Canonical deployments always migrate. A staging PR preview migrates only when
`DATABASE_IS_EPHEMERAL=true`, confirming that it does not share the canonical
staging database. Legacy deployments without `DEPLOYMENT` are treated as
canonical for backwards compatibility.

Container entrypoints can use the same decision without application-specific
shell logic:

```bash
if python3 -m fchelper.deployment should-migrate; then
  python3 manage.py migrate
fi
```
