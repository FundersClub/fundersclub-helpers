import os

import requests


def runtime_load_balancer_hosts(*, include_imds=False, timeout=2):
    """Return the runtime IP(s) to add to Django ALLOWED_HOSTS so an upstream load
    balancer's by-IP health check passes.

    - ECS Fargate: reads the task IP from ``$ECS_CONTAINER_METADATA_URI_V4`` (auto-injected
      on Fargate, so this branch is a no-op elsewhere).
    - EC2 / Elastic Beanstalk: reads the instance IP from IMDS when ``include_imds=True``
      (callers gate this on their own ``LOAD_BALANCED`` setting).

    Never raises; returns a list (possibly empty).

    Usage in settings.py::

        from fchelper.hosts import runtime_load_balancer_hosts

        ALLOWED_HOSTS += runtime_load_balancer_hosts(
            include_imds=setting("LOAD_BALANCED", "false") == "true"
        )
    """
    ecs_uri = os.getenv("ECS_CONTAINER_METADATA_URI_V4")
    if ecs_uri:
        try:
            return [requests.get(ecs_uri, timeout=timeout).json()["Networks"][0]["IPv4Addresses"][0]]
        except (requests.exceptions.RequestException, KeyError, IndexError, ValueError):
            return []

    if include_imds:
        try:
            ip = requests.get("http://169.254.169.254/latest/meta-data/local-ipv4", timeout=timeout).text.strip()
        except requests.exceptions.RequestException:
            return []
        return [ip] if ip else []

    return []
