import io
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from fake_ollama import running
from stack_canary import harness_hermes, managed
from stack_canary.cli import main
from stack_canary.config import Settings
from stack_canary.harness_hermes import HermesHarness, hermes_env, hermes_tags
from test_runtime_ollama import home, managed_fixture, port, read_reports, settings

SOURCE = Path(__file__).with_name("fake_hermes.py")
TAG = "v0.23.1"


def fake_harness(root, tag, settings, guard, *, mode="ok"):
    def install(root, tag, guard):
        relative = Path("harnesses/hermes") / tag / "venv"
        bindir = root / relative / ("Scripts" if os.name == "nt" else "bin")
        bindir.mkdir(parents=True, exist_ok=True)
        script = bindir / "hermes.py"
        shutil.copyfile(SOURCE, script)
        script.with_name("mode.txt").write_text(mode)
        return script, {"tag": tag, "commit": "fake", "python_version": sys.version.split()[0]}
    return HermesHarness(root, tag, settings, guard, installer=install,
                         argv_factory=lambda executable: [sys.executable, "-B", str(executable)])


@pytest.fixture(autouse=True)
def use_numeric_loopback(monkeypatch):
    monkeypatch.setattr(harness_hermes, "_local_ollama_name", lambda: False)


def test_release_filtering():
    def fetch(url, *, headers):
        rows = [
            {"tag_name": TAG}, {"tag_name": "v0.23.0"}, {"tag_name": "v2026.11.1"},
            {"tag_name": "v0.24.0+canary", "prerelease": True}, {"tag_name": "v0.20.0"},
        ]
        return io.BytesIO(json.dumps(rows if "?" in url else rows[0]).encode())

    assert hermes_tags("latest", fetch=fetch) == [TAG]
    assert hermes_tags("last:2", fetch=fetch) == [TAG, "v0.23.0"]
    assert hermes_tags(TAG, fetch=fetch) == [TAG]


def test_environment_uses_isolated_paths(home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "user")
    monkeypatch.setenv("HERMES_HOME", "user-hermes")
    monkeypatch.setenv("OLLAMA_HOST", "user-ollama")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "user-secret")
    work = home / "work"
    env = hermes_env(home, TAG, work)
    assert set(key for key in env if key.startswith("HERMES_")) == {"HERMES_HOME", "HERMES_WRITE_SAFE_ROOT"}
    assert all(key not in env for key in ("OLLAMA_HOST", "ANTHROPIC_API_KEY"))
    assert Path(env["HOME"]).is_relative_to(home)
    assert Path(env["TMPDIR"]).is_relative_to(Path(env["HOME"]))
    assert Path(env["HERMES_WRITE_SAFE_ROOT"]) == work


@pytest.mark.parametrize(("mode", "reason"), [
    ("ok", None), ("wrong_content", "file_mismatch"), ("no_file", "file_missing"),
    ("no_write_event", "no_write_event"), ("nonzero_exit", "harness_exit_nonzero"),
    ("hang", "timeout_deadline"), ("unsupported_cli", "harness_cli_unsupported"),
    ("leftover_child", None),
])
def test_b6_attempt_modes(home, settings, mode, reason):
    settings = replace(settings, hermes_deadline_s=0.5 if mode == "hang" else 6)
    harness = fake_harness(home, TAG, settings, lambda _: None, mode=mode)
    with running() as upstream:
        attempt = harness.attempt(upstream.endpoint, "a", 1)
    assert attempt["reason"] == reason
    assert attempt["attribution"] == ("harness" if reason else None)
    assert harness_hermes.API_KEY not in attempt["argv"]
    if mode == "leftover_child":
        child, = [event["pid"] for event in attempt["events"] if event["type"] == "fixture_child"]
        assert child in attempt["leftover_processes"]
    if mode == "ok":
        exchange, = attempt["proxy_exchanges"]
        assert exchange["response"]["status"] == 200
        assert ("Authorization", "[redacted]") in exchange["request"]["headers"]
        assert attempt["file_bytes"].startswith("STACK-CANARY-")


def test_b6_attributes_upstream_http_error(home, settings):
    harness = fake_harness(home, TAG, replace(settings, hermes_deadline_s=6), lambda _: None)
    with running("http_500") as upstream:
        attempt = harness.attempt(upstream.endpoint, "a", 1)
    assert attempt["outcome"] == "fail"
    assert attempt["attribution"] == "runtime"
    assert attempt["proxy_exchanges"][0]["response"]["status"] == 500


def test_managed_b6_end_to_end(home, settings, monkeypatch):
    managed_fixture(monkeypatch, settings)
    monkeypatch.setattr(managed, "hermes_tags", lambda spec, **kwargs: [TAG])
    monkeypatch.setattr(managed, "HermesHarness", fake_harness)
    args = ["run", "--ollama", "last:1", "--hermes", "latest", "--models", "x",
            "--checks", "B6", "--repeats", "1"]
    assert main(args) == 0
    result = read_reports(home)["1.10.0"]
    assert result["harness"]["tag"] == TAG
    assert result["harness"]["commit"] == "fake"
    check, = result["models"][0]["checks"]
    assert check["check_id"] == "B6"
    assert check["outcome"] == "pass"
    assert check["attempts"][0]["proxy_exchanges"][0]["response"]["status"] == 200
