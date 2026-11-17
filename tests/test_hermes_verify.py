from types import SimpleNamespace

import pytest

from stack_canary import hermes_registry, hermes_verify


def test_static_contract(tmp_path):
    cli = tmp_path / "hermes_cli"
    cli.mkdir()
    (cli / "gateway_client.py").write_text(
        "def connect_gateway(endpoint=None):\n"
        "    if endpoint is not None:\n"
        "        return _session_ticket(endpoint)\n"
        "    return ensure_gateway_runtime()\n")
    (cli / "gateway_chat.py").write_text("def run_gateway_chat():\n    connect_gateway()\n")
    (cli / "gateway_runtime.py").write_text(
        "def discover_gateway_endpoint(): pass\n"
        "def ensure_gateway_runtime(): pass\n"
        'def commands(): return ["launchctl", "/bin/launchctl"]\n')
    constants = tmp_path / "hermes_constants.py"
    constants.write_text('def _iter_real_home_candidates(): return [env("HERMES_REAL_HOME")]\n'
                         "def get_real_home(): pass\n")
    result = hermes_verify.static_check(tmp_path)
    assert result["passed"] and result["launchctl"] == ["PATH", "absolute"]
    constants.write_text("def get_real_home(): pass\n")
    assert not hermes_verify.static_check(tmp_path)["passed"]


@pytest.mark.parametrize(("new", "shim", "status"), [(False, False, "verified"),
                                                      (True, False, "unsupported"),
                                                      (False, True, "unsupported")])
def test_attestation_and_job_cleanup(tmp_path, monkeypatch, new, shim, status):
    tag, commit = "v0.23.3", "a" * 40
    exe = tmp_path / "harnesses/hermes" / tag / "venv/bin/hermes"
    job = "com.nousresearch.hermes.runtime.new"
    snapshots = iter([{"launchd": {"ai.hermes.gateway": 7}, "launch_agents": {}},
                      {"launchd": {"ai.hermes.gateway": 7, **({job: None} if new else {})},
                       "launch_agents": {}}])
    monkeypatch.setattr(hermes_verify, "available", lambda: True)
    monkeypatch.setattr(hermes_verify.isolation, "snapshot", lambda: next(snapshots))
    monkeypatch.setattr(hermes_verify, "hermes_tags", lambda *a, **kw: [tag])
    monkeypatch.setattr(hermes_verify, "_install", lambda *a: (exe, {"commit": commit}))
    monkeypatch.setattr(hermes_verify, "static_check", lambda p: {"passed": True})

    def attempt(runtime, tag, installed, log):
        assert (log.parent / "launchctl").is_file()
        if shim:
            log.write_text("attempt")
        return {"outcome": "pass"}

    calls = []

    def launch(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=f"HERMES_HOME => {tmp_path / 'probe'}\n")

    monkeypatch.setattr(hermes_verify, "_probe", attempt)
    monkeypatch.setattr(hermes_verify, "_launchctl", launch)
    runtime = SimpleNamespace(root=tmp_path, guard=lambda op: None, fetch=None)
    row = hermes_verify.verify(runtime, tag)
    assert row["status"] == status and row["evidence"]["shim_empty"] is not shim
    assert [action for action, _ in calls] == (["print", "bootout"] if new else [])
    assert hermes_registry.verified_commit(tmp_path, tag) == (commit if status == "verified" else None)
    assert not hermes_verify._owned_job(tmp_path, "HERMES_HOME => /Users/someone/.hermes")
