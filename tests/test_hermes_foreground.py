import json
from types import ModuleType, SimpleNamespace

import pytest

from stack_canary import hermes_foreground as foreground


def test_gateway_runs_as_child_and_is_reaped(tmp_path, monkeypatch):
    endpoint = SimpleNamespace(supervisor="none", control_home=None)
    gateway = SimpleNamespace(pid=319, poll=lambda: None)
    events = []
    gateway.terminate = lambda: events.append("terminate")
    gateway.wait = lambda *, timeout: events.append(("wait", timeout))
    (tmp_path / "gateway.pid").write_text(json.dumps({"pid": gateway.pid}), encoding="utf-8")

    def popen(argv, **kwargs):
        events.append(argv)
        return gateway

    def discover(home, *, timeout):
        assert home == tmp_path.resolve()
        return SimpleNamespace(state="ready", endpoint=endpoint)

    upstream = ModuleType("hermes_cli.gateway_runtime")
    upstream.discover_gateway_endpoint = discover
    monkeypatch.setitem(foreground.sys.modules, upstream.__name__, upstream)
    monkeypatch.setattr(foreground, "os", SimpleNamespace(
        environ={"HERMES_HOME": str(tmp_path)}, fsencode=lambda _: b"short-path",
    ))
    monkeypatch.setattr(foreground.subprocess, "Popen", popen)
    monkeypatch.setattr(foreground, "_attach_chat", lambda home, owner: events.append(owner) or 0)
    assert foreground.run(["/canary/hermes", "chat", "--oneshot"], 2, tmp_path / "gateway.log") == 0
    assert events == [["/canary/hermes", "gateway", "run", "--force"], endpoint, "terminate", ("wait", 3)]

    events.clear()
    endpoint.supervisor = "launchd"
    with pytest.raises(RuntimeError, match="no longer owns"):
        foreground.run(["/canary/hermes", "chat", "--oneshot"], 2, tmp_path / "gateway.log")
    assert events == [["/canary/hermes", "gateway", "run", "--force"], "terminate", ("wait", 3)]


def test_dead_foreground_gateway_never_attempts_discovery(tmp_path):
    process = SimpleNamespace(pid=319, poll=lambda: 1)
    with pytest.raises(RuntimeError, match="did not become ready"):
        foreground._ready_endpoint(process, tmp_path, 0.1, lambda **_: pytest.fail("unexpected discovery"))


def test_chat_uses_only_explicit_endpoint_even_when_attach_fails(tmp_path, monkeypatch):
    modules = {name: ModuleType(name) for name in (
        "hermes_cli", "hermes_cli.gateway_chat", "hermes_cli.gateway_client",
        "hermes_cli.gateway_runtime", "hermes_cli.main", "hermes_constants",
    )}
    for name, module in modules.items():
        monkeypatch.setitem(foreground.sys.modules, name, module)
    upstream = modules["hermes_cli"]
    upstream.gateway_chat = modules["hermes_cli.gateway_chat"]
    upstream.gateway_runtime = modules["hermes_cli.gateway_runtime"]
    modules["hermes_constants"].get_hermes_home = lambda: tmp_path
    owner = object()

    def connect(*, endpoint):
        assert endpoint is owner
        raise ConnectionError("owner went away")

    def main():
        with pytest.raises(RuntimeError, match="Foreground Hermes gateway required"):
            upstream.gateway_runtime.ensure_gateway_runtime(tmp_path)
        return upstream.gateway_chat.connect_gateway()

    modules["hermes_cli.gateway_client"].connect_gateway = connect
    modules["hermes_cli.gateway_client"].GatewayClientError = RuntimeError
    modules["hermes_cli.main"].main = main
    with pytest.raises(ConnectionError, match="owner went away"):
        foreground._attach_chat(tmp_path.resolve(), owner)
