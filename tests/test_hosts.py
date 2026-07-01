import requests

from fchelper.hosts import runtime_load_balancer_hosts


def test_ecs_metadata_present_returns_task_ip(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")

    class _Resp:
        def json(self):
            return {"Networks": [{"IPv4Addresses": ["10.0.4.33"]}]}

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    assert runtime_load_balancer_hosts() == ["10.0.4.33"]


def test_ecs_metadata_present_but_malformed_returns_empty(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")

    class _Resp:
        def json(self):
            return {"Networks": []}  # missing IPv4Addresses entirely

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    assert runtime_load_balancer_hosts() == []


def test_ecs_metadata_present_but_unreachable_returns_empty(monkeypatch):
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")

    def _raise(*a, **k):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", _raise)

    assert runtime_load_balancer_hosts() == []


def test_ecs_metadata_present_takes_precedence_over_imds(monkeypatch):
    # Even if include_imds=True, a Fargate task must not also query EC2 IMDS.
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")

    class _Resp:
        def json(self):
            return {"Networks": [{"IPv4Addresses": ["10.0.4.33"]}]}

    calls = []

    def _get(url, *a, **k):
        calls.append(url)
        return _Resp()

    monkeypatch.setattr(requests, "get", _get)

    assert runtime_load_balancer_hosts(include_imds=True) == ["10.0.4.33"]
    assert calls == ["http://169.254.170.2/v4/abc"]


def test_no_ecs_metadata_and_include_imds_false_makes_no_request(monkeypatch):
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)

    def _raise(*a, **k):
        raise AssertionError("should not make a network call")

    monkeypatch.setattr(requests, "get", _raise)

    assert runtime_load_balancer_hosts(include_imds=False) == []


def test_no_ecs_metadata_and_include_imds_true_returns_instance_ip(monkeypatch):
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)

    class _Resp:
        text = "10.1.2.3"

    def _get(url, *a, **k):
        assert url == "http://169.254.169.254/latest/meta-data/local-ipv4"
        return _Resp()

    monkeypatch.setattr(requests, "get", _get)

    assert runtime_load_balancer_hosts(include_imds=True) == ["10.1.2.3"]


def test_no_ecs_metadata_and_imds_unreachable_returns_empty(monkeypatch):
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)

    def _raise(*a, **k):
        raise requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(requests, "get", _raise)

    assert runtime_load_balancer_hosts(include_imds=True) == []
