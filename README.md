# fundersclub-helpers

Helpers for the Funder's Club applications. Usable as a Python package.

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
