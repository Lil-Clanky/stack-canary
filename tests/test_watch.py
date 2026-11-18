import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from stack_canary import managed, report, watch
from stack_canary.cli import main
from stack_canary.config import Settings
from stack_canary.harness_hermes import API
from stack_canary.releases import API_URL
from stack_canary.runtime_ollama import OllamaRuntime
from test_harness_hermes import fake_harness
from test_releases import release_record
from test_runtime_ollama import home, managed_fixture, port, settings


TAG = "v0.23.2"


def _result(version, *, fail=None, error=False):
    cells = [{"check_id": check, "outcome": "fail" if check == fail else "pass", "attempts": []}
             for check in report.CHECK_IDS]
    cells[-1]["harness_tag"] = TAG
    data = {"runtime": {"version": version}, "hermes_tags": [TAG],
            "models": [{"name": "x", "checks": cells}], "isolation": {"status": "pass"}}
    if error:
        data["infra_error"] = {"code": "install_failed", "message": "fixture install error"}
    return data


def _history(home, *records):
    directory = home / "results"
    directory.mkdir(parents=True, exist_ok=True)
    for index, record in enumerate(records):
        (directory / f"20261115T12000{index}Z.json").write_text(json.dumps(record), encoding="utf-8")


def _summary():
    return json.loads(Path("report/summary.json").read_text(encoding="utf-8"))


@pytest.fixture
def offline(monkeypatch, home):
    Path("canary.toml").write_text('models = ["x"]\n', encoding="utf-8")
    monkeypatch.setattr(watch.isolation, "snapshot", lambda: {})
    monkeypatch.setattr(watch.hermes_verify, "available", lambda: False)
    monkeypatch.setattr(watch, "_swap_used_bytes", lambda: None)
    tags = [{"tag_name": TAG}]
    calls = []
    checks_run = []

    def fetch(url, *, headers):
        if url == API_URL + "?per_page=50":
            return io.BytesIO(json.dumps([release_record("1.2.3"), release_record("1.2.2")]).encode())
        assert url == API + "?per_page=50"
        return io.BytesIO(json.dumps(tags).encode())

    def run(runtime, names, checks, output, before, resolve, reports):
        assert names == ("x",)
        checks_run.append(checks)
        assert (runtime.root / ".run-lock/owner.json").exists()
        releases, requested = resolve()
        assert requested == ([TAG] if "B6" in checks else [])
        for release in releases:
            calls.append(release.version)
            data = _result(release.version, fail="A2")
            reports.append(data)
            managed.results.write(output, data, root=home, version=release.version)
        runtime.disk_trace.extend([
            {"free_bytes": 10000, "swap_used_bytes": 150},
            {"free_bytes": 5000, "swap_used_bytes": 300},
        ])
        return 1

    monkeypatch.setattr(managed, "fetch_url", fetch)
    monkeypatch.setattr(managed, "_run_with_isolation", run)
    return tags, calls, checks_run


def test_nothing_new(offline, home, capsys):
    _history(home, _result("1.2.2"), _result("1.2.3"))
    assert main(["watch"]) == 0
    assert "nothing new" in capsys.readouterr().out
    assert offline[1] == []
    assert _summary()["status"] == "nothing new"


@pytest.mark.parametrize("usable_b6", [True, False])
def test_watch_merges_separate_api_and_tagged_b6_files(offline, home, usable_b6):
    a = _result("1.2.3")
    a["models"][0]["checks"].pop()
    b = _result("1.2.3")
    b["models"][0]["checks"] = b["models"][0]["checks"][-1:]
    if not usable_b6:
        b["isolation"] = {"status": "isolation_violated"}
    _history(home, _result("1.2.2"), a, b)
    assert main(["watch"]) == (0 if usable_b6 else 1)
    assert offline[1] == ([] if usable_b6 else ["1.2.3"])
    assert offline[2] == ([] if usable_b6 else [("B6",)])


def test_watch_runs_only_missing_checks_and_models(offline, home):
    Path("canary.toml").write_text('models = ["x", "y"]\n', encoding="utf-8")
    complete, partial = _result("1.2.2"), _result("1.2.3")
    for data in (complete, partial):
        data["models"].append({**data["models"][0], "name": "y"})
    # Only x lacks A6, so the fake runner's names == ("x",) assertion also covers model selection.
    partial["models"][0]["checks"] = [cell for cell in partial["models"][0]["checks"] if cell["check_id"] != "A6"]
    _history(home, complete, partial)
    main(["watch"])
    assert offline[1] == ["1.2.3"]
    assert offline[2] == [("A6",)]


@pytest.mark.parametrize("previous_error", [False, True])
def test_new_or_incomplete_ollama_and_summary(offline, home, previous_error):
    old = [_result("1.2.2")]
    if previous_error:
        old.append(_result("1.2.3", error=True))
    _history(home, *old)
    assert main(["watch"]) == 1
    assert offline[1] == ["1.2.3"]
    summary = _summary()
    assert summary["ran"] == ["1.2.3"]
    assert summary["regressions"] == [
        {"version": "1.2.3", "previous_version": "1.2.2", "model": "x", "check": "A2", "outcome": "fail"},
    ]
    assert summary["lowest_free_bytes"] == 5000 and summary["peak_swap_used_bytes"] == 300
    assert summary["duration_s"] >= 0
    assert "1.2.3 vs 1.2.2, x A2: fail" in Path("report/summary.md").read_text(encoding="utf-8")
    assert Path("report/index.html").is_file()


def test_dry_run_and_unverified_tag_notification_once(offline, home, monkeypatch):
    _history(home, _result("1.2.2"), _result("1.2.3"))
    tags, calls, _ = offline
    tags.append({"tag_name": "v0.23.3"})
    Path("canary.toml").write_text('models = ["x"]\nnotify_command = ["fixture", "--flag"]\n', encoding="utf-8")
    notifications = []
    monkeypatch.setattr(watch.subprocess, "run", lambda argv, **kwargs: notifications.append((argv, kwargs)))
    assert main(["watch", "--dry-run"]) == 0
    assert not (home / "release-cache").exists() and not Path("report").exists()
    assert notifications == []
    assert main(["watch"]) == 0
    assert _summary()["unverified_hermes"] == _summary()["new_unverified_hermes"] == ["v0.23.3"]
    assert main(["watch"]) == 0
    assert _summary()["unverified_hermes"] == ["v0.23.3"]
    assert _summary()["new_unverified_hermes"] == []
    assert calls == []
    [(argv, kwargs)] = notifications
    assert argv == ["fixture", "--flag", str(Path("report/summary.md").resolve())]
    assert kwargs["timeout"] == watch.HOOK_TIMEOUT_S


def test_live_pid_and_real_guard_skip_watch(offline, home, capsys):
    lock = home / ".run-lock"
    lock.mkdir(parents=True)
    owner = lock / "owner.json"
    owner.write_text(json.dumps({"pid": os.getpid(), "started_at": "fixture"}), encoding="utf-8")
    assert main(["watch"]) == 0
    assert owner.is_file()
    owner.unlink()
    lock.rmdir()
    with OllamaRuntime(home, Settings()).exclusive():
        assert main(["watch"]) == 0
        assert owner.is_file()
    assert not lock.exists()
    assert capsys.readouterr().out.count("already running") == 2
    assert offline[1] == []


def test_stale_pid_is_recovered(offline, home, monkeypatch, capsys):
    _history(home, _result("1.2.2"), _result("1.2.3"))
    lock = home / ".run-lock"
    lock.mkdir()
    (lock / "owner.json").write_text(json.dumps({"pid": 12345, "started_at": "fixture"}), encoding="utf-8")
    monkeypatch.setattr("stack_canary.runtime_ollama._pid_alive", lambda pid: pid != 12345)
    assert main(["watch"]) == 0
    assert "recovering stale run lock from PID 12345" in capsys.readouterr().out
    assert not lock.exists()


@pytest.mark.parametrize(("field", "code", "success", "notified"), [
    ("regressions", 1, False, True),
    ("infra_errors", 2, False, True),
    ("isolation_errors", 2, False, True),
    ("new_unverified_hermes", 0, False, True),
    (None, 0, True, True),
    (None, 0, False, False),
])
def test_notification_conditions(field, code, success, notified, monkeypatch):
    summary = dict.fromkeys(("regressions", "infra_errors", "isolation_errors", "new_unverified_hermes"), [])
    if field:
        summary[field] = ["fixture"]
    calls = []
    monkeypatch.setattr(watch.subprocess, "run", lambda *args, **kwargs: calls.append(args))
    watch._notify(Settings(notify_command=("fixture",), notify_on_success=success), summary, Path("summary.md"), code)
    assert bool(calls) == notified


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("fixture", 15), subprocess.CalledProcessError(7, "fixture")])
def test_notification_failure_preserves_exit(offline, home, monkeypatch, capsys, error):
    _history(home, _result("1.2.2"))
    Path("canary.toml").write_text('models = ["x"]\nnotify_command = ["fixture"]\n', encoding="utf-8")

    def run(argv, **kwargs):
        assert kwargs["timeout"] == 15 and kwargs["check"]
        raise error

    monkeypatch.setattr(watch.subprocess, "run", run)
    assert main(["watch"]) == 1
    assert "notification hook failed" in capsys.readouterr().err


def test_watch_uses_managed_runner_end_to_end(home, settings, monkeypatch):
    managed_fixture(monkeypatch, settings)
    original = managed.fetch_url

    def fetch(url, *, headers):
        if url == API + "?per_page=50":
            return io.BytesIO(json.dumps([{"tag_name": TAG}]).encode())
        return original(url, headers=headers)

    monkeypatch.setattr(managed, "fetch_url", fetch)
    monkeypatch.setattr(watch, "OllamaRuntime", managed.OllamaRuntime)
    monkeypatch.setattr(managed, "HermesHarness", fake_harness)
    with Path("canary.toml").open("a", encoding="utf-8") as config:
        config.write('models = ["x"]\nrepeats = 1\n')
    assert main(["watch"]) == 0
    assert _summary()["ran"] == ["1.10.0"]
    assert main(["watch"]) == 0
    assert _summary()["status"] == "nothing new"


def test_watch_summary_retains_infra_and_isolation_errors(offline, home, monkeypatch):
    _history(home, _result("1.2.2"))

    def run(runtime, names, checks, output, before, resolve, reports):
        data = _result("1.2.3", error=True)
        data["isolation"] = {"status": "isolation_violated", "diff": {"launch_agents": {}}}
        reports.append(data)
        return 2

    monkeypatch.setattr(managed, "_run_with_isolation", run)
    assert main(["watch"]) == 2
    summary = _summary()
    assert summary["infra_errors"][0]["code"] == "install_failed"
    assert summary["isolation_errors"][0]["diff"] == {"launch_agents": {}}
    assert summary["exit_code"] == 2
