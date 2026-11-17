import io
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from fake_ollama import running
from stack_canary import harness_hermes, hermes_foreground, managed
from stack_canary.cli import main
from stack_canary.config import Settings
from stack_canary.harness_hermes import HermesHarness, hermes_env, hermes_tags
from stack_canary.runtime_ollama import OllamaError
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
    assert set(key for key in env if key.startswith("HERMES_")) == {
        "HERMES_HOME", "HERMES_REAL_HOME", "HERMES_WRITE_SAFE_ROOT",
    }
    assert all(key not in env for key in ("OLLAMA_HOST", "ANTHROPIC_API_KEY"))
    assert Path(env["HOME"]).is_relative_to(home)
    assert env["HOME"] == env["HERMES_REAL_HOME"]
    assert Path(env["HOME"]) != Path(env["HERMES_HOME"]) / "home"
    assert Path(env["TMPDIR"]).is_relative_to(Path(env["HOME"]))
    assert Path(env["HERMES_WRITE_SAFE_ROOT"]) == work


@pytest.mark.parametrize("has_lock", [True, False])
def test_install_uses_editable_checkout(home, monkeypatch, has_lock):
    tag = "v0.23.2"
    prefix = home / "harnesses/hermes" / tag
    src, venv = prefix / "src", prefix / "venv"
    interpreter = home / "python3.14"
    for directory in (src, venv):
        directory.mkdir(parents=True)
        (directory / "partial").touch()
    (prefix / ".complete.part").touch()
    commands = []

    def run(argv, cwd, env, *, timeout):
        if argv[:2] == ["git", "clone"]:
            assert not src.exists() and not venv.exists() and not (prefix / ".complete.part").exists()
            src.mkdir()
            (src / "pyproject.toml").write_text("[project.optional-dependencies]\nall = []\n")
            (src / "pm").mkdir()
            (src / "pm/lock.json").write_text('{"packages":{"python":{"version":"3.14.9+20261021"}}}')
            if has_lock:
                (src / "uv.lock").touch()
        elif argv[:2] == ["git", "rev-parse"]:
            return "ae2a81c"
        elif argv[0] == "uv":
            assert cwd == src
            assert env["UV_PROJECT_ENVIRONMENT"] == str(venv)
            assert Path(env["UV_CACHE_DIR"]).is_relative_to(home)
            assert Path(env["UV_PYTHON_INSTALL_DIR"]).is_relative_to(home)
            assert Path(env["HOME"]).is_relative_to(home)
            commands.append(argv[1:])
            if argv[1] == "pip" and "-e" not in argv:
                raise RuntimeError("Building wheels or sdists for hermes-agent is not supported")
            executable = harness_hermes._bin(venv, "hermes")
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.touch()
        elif argv[1] == "--version":
            return "Python 3.14.9"
        else:
            pytest.fail(f"unexpected command: {argv}")

    monkeypatch.setattr(harness_hermes, "_run", run)
    monkeypatch.setattr(harness_hermes.shutil, "which", lambda _: "uv")

    def select(minor, cwd, env, uv):
        assert (minor, cwd, uv) == ("3.14", src, "uv")
        return str(interpreter)

    monkeypatch.setattr(harness_hermes, "_interpreter", select)
    _, metadata = harness_hermes._install(home, tag, lambda _: None)
    if has_lock:
        assert commands == [["sync", "--frozen", "--no-default-groups", "--python",
                             str(interpreter), "--extra", "all"]]
        assert metadata["install_method"] == "uv_sync_editable"
    else:
        assert commands[-1][-2:] == ["-e", f"{src}[all]"]
        assert metadata["install_method"] == "uv_pip_editable"
    assert json.loads((prefix / ".complete").read_text()) == metadata
    assert metadata["python_version"] == "Python 3.14.9"


def test_gateway_flags_intersect_help_and_source(home):
    home.mkdir()
    source = home / "gateway_chat.py"
    source.write_text('_UNSUPPORTED = ("run_budget", "max_turns", "w")\n', encoding="utf-8")
    help_text = "--oneshot --provider --base-url -m -t --query-file --format stream-json --run-budget --max-turns --yolo"
    allowed = harness_hermes._gateway_options(home, source)
    assert "--in" in allowed
    actual = harness_hermes.detected_flags(help_text, allowed)
    assert actual == {"--oneshot", "--provider", "--base-url", "-m", "-t", "--query-file", "--format", "--yolo"}
    assert harness_hermes.detected_flags(help_text, {"-m", "--in"}) == {"-m"}


def test_interpreter_prefers_matching_parent_path(home, monkeypatch):
    older, correct = home / "mislabeled", home / "python"
    monkeypatch.setattr(harness_hermes.shutil, "which",
                        {"python3.14": str(older), "python": str(correct)}.get)
    commands = []

    def run(argv, cwd, env, *, timeout):
        commands.append(argv)
        return "Python 3.13.4" if argv[0] == str(older) else "Python 3.14.2"

    monkeypatch.setattr(harness_hermes, "_run", run)
    assert harness_hermes._interpreter("3.14", home, {}, "uv") == str(correct)
    assert commands == [[str(older), "--version"], [str(correct), "--version"]]


def test_interpreter_installs_minor_in_canary_home(home, monkeypatch):
    monkeypatch.setattr(harness_hermes.shutil, "which", lambda _: None)
    directory = home / "uv-python"
    executable = directory / "cpython-3.14.2" / "python"
    env = {"UV_PYTHON_INSTALL_DIR": str(directory)}
    commands = []

    def run(argv, cwd, passed_env, *, timeout):
        assert passed_env == env
        commands.append(argv)
        if argv[0] == str(executable):
            return "Python 3.14.2"
        if argv[2] == "install":
            executable.parent.mkdir(parents=True)
            executable.touch()
            return ""
        return str(executable)

    monkeypatch.setattr(harness_hermes, "_run", run)
    assert harness_hermes._interpreter("3.14", home, env, "uv") == str(executable)
    assert commands == [
        ["uv", "python", "install", "--no-bin", "--no-registry", "3.14"],
        ["uv", "python", "find", "--managed-python", "--no-project", "3.14"],
        [str(executable), "--version"],
    ]


@pytest.mark.parametrize("uv", [None, "uv"])
def test_interpreter_failure_is_a_clear_setup_error(home, monkeypatch, uv):
    monkeypatch.setattr(harness_hermes.shutil, "which", lambda _: None)

    def fail(*args, **kwargs):
        raise OSError("fixture interpreter install failed")

    monkeypatch.setattr(harness_hermes, "_run", fail)
    with pytest.raises(OllamaError, match="Python 3.14") as error:
        harness_hermes._interpreter("3.14", home, {}, uv)
    assert error.value.code == "hermes_setup_failed"
    assert ("fixture interpreter install failed" if uv else "uv is unavailable") in str(error.value)


def test_failed_install_leaves_no_completion_marker(home, monkeypatch):
    prefix = home / "harnesses/hermes/v0.23.2"
    prefix.mkdir(parents=True)
    (prefix / ".complete").touch()
    (prefix / ".complete.part").touch()

    def run(argv, cwd, env, *, timeout):
        if argv[0] == "git":
            return "ae2a81c"
        pytest.fail(f"unexpected command: {argv}")

    def fail(*args):
        raise OSError("fixture dependency failure")

    monkeypatch.setattr(harness_hermes, "_run", run)
    monkeypatch.setattr(harness_hermes, "_dependencies", fail)
    with pytest.raises(OSError, match="fixture dependency failure"):
        harness_hermes._install(home, "v0.23.2", lambda _: None)
    assert not (prefix / ".complete").exists()
    assert not (prefix / ".complete.part").exists()


def test_cached_harness_rebuilds_when_venv_python_disappears(home, monkeypatch):
    tag = "v0.23.2"
    prefix = home / "harnesses/hermes" / tag
    src, venv = prefix / "src", prefix / "venv"
    src.mkdir(parents=True)
    executable = harness_hermes._bin(venv, "hermes")
    executable.parent.mkdir(parents=True)
    executable.touch()
    marker = prefix / ".complete"
    marker.write_text(json.dumps({"install_method": "uv_sync_editable", "python_version": "Python 3.14.9"}))

    def run(argv, cwd, env, *, timeout):
        if argv[-1] == "--version":
            raise FileNotFoundError("the venv's interpreter is gone")
        assert argv[:2] == ["git", "clone"]
        assert not src.exists() and not venv.exists() and not marker.exists()
        raise OSError("fixture reached fresh clone")

    monkeypatch.setattr(harness_hermes, "_run", run)
    with pytest.raises(OSError, match="fixture reached fresh clone"):
        harness_hermes._install(home, tag, lambda _: None)


@pytest.mark.parametrize(("mode", "reason"), [
    ("ok", None), ("wrong_content", "file_mismatch"), ("no_file", "file_missing"),
    ("no_write_event", "no_write_event"), ("nonzero_exit", "harness_exit_nonzero"),
    ("hang", "harness_not_started"), ("unsupported_cli", "harness_cli_unsupported"),
    ("draining", "harness_not_started"),
    ("leftover_child", None),
])
def test_b6_attempt_modes(home, settings, mode, reason):
    settings = replace(settings, hermes_deadline_s=0.5 if mode == "hang" else 6)
    harness = fake_harness(home, TAG, settings, lambda _: None, mode=mode)
    with running() as upstream:
        attempt = harness.attempt(upstream.endpoint, "a", 1)
    assert attempt["reason"] == reason
    assert attempt["attribution"] == ("harness" if reason else None)
    if mode in {"hang", "unsupported_cli", "draining"}:
        assert attempt["outcome"] == "error"
        assert attempt["note"] == "before_first_request"
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


@pytest.mark.parametrize(("mode", "outcome", "attribution"), [
    ("probe_pass", "pass", None), ("probe_fail", "fail", "harness"),
])
def test_b6_does_not_blame_ollama_for_probe_404(home, settings, mode, outcome, attribution):
    harness = fake_harness(home, TAG, settings, lambda _: None, mode=mode)
    with running() as upstream:
        attempt = harness.attempt(upstream.endpoint, "a", 1)
    assert attempt["outcome"] == outcome
    assert attempt["attribution"] == attribution
    probe, completion = attempt["proxy_exchanges"]
    assert probe["request"]["path"] == "/api/v1/models"
    assert probe["response"]["status"] == 404
    assert completion["response"]["status"] == 200


def test_unverified_macos_tag_cannot_install_or_probe(home, settings, monkeypatch, capsys):
    monkeypatch.setattr(harness_hermes, "sys", type("OS", (), {"platform": "darwin", "stderr": sys.stderr}))
    harness = HermesHarness(home, TAG, settings, lambda _: None,
                            installer=lambda *args: pytest.fail("unexpected install"))
    assert harness.metadata["gateway_mode"] == "disabled"
    assert harness.check("unused", "a") == {
        "check_id": "B6", "outcome": "error", "reason": "harness_requires_launchd",
        "attribution": "harness", "attempts": [],
    }
    assert "B6 disabled on macOS" in capsys.readouterr().err
    assert not home.exists()


def test_audited_macos_commit_uses_foreground(home, settings, monkeypatch):
    monkeypatch.setattr(harness_hermes, "sys", type("OS", (), {"platform": "darwin"}))
    launcher = harness_hermes._bin(home / "harnesses/hermes/v0.23.2/venv", "hermes")
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    calls = []

    def help_probe(argv, cwd, env, *, timeout):
        calls.append(argv)
        assert Path(env["HERMES_REAL_HOME"]).is_relative_to(home)
        return "--oneshot --provider --base-url -m -t --query-file"

    monkeypatch.setattr(harness_hermes, "_run", help_probe)
    harness = HermesHarness(home, hermes_foreground.TAG, settings, lambda _: None,
                            installer=lambda *args: (launcher, {"commit": hermes_foreground.COMMIT}))
    assert harness.metadata["gateway_mode"] == "foreground"
    assert calls == [[str(launcher), "chat", "--help"]]
    env = harness._env(home / "work", profile=Path("attempt/h"))
    assert Path(env["HERMES_GATEWAY_LOCK_DIR"]) == home / "attempt/h/locks"


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
    steps = [sample["step"] for sample in result["disk_trace"]]
    assert "before Hermes install v0.23.1" in steps
    assert "after Hermes install v0.23.1" in steps
    check, = result["models"][0]["checks"]
    assert check["check_id"] == "B6"
    assert check["outcome"] == "pass"
    assert check["attempts"][0]["proxy_exchanges"][0]["response"]["status"] == 200


def test_rejected_hermes_invocation_is_an_error_without_a_repro(home, settings, monkeypatch):
    managed_fixture(monkeypatch, settings)
    monkeypatch.setattr(managed, "hermes_tags", lambda spec, **kwargs: [TAG])
    monkeypatch.setattr(managed, "HermesHarness",
                        lambda *args: fake_harness(*args, mode="cli_usage"))
    assert main(["run", "--ollama", "last:1", "--hermes", "latest", "--models", "x",
                 "--checks", "B6", "--repeats", "1"]) == 2
    check, = read_reports(home)["1.10.0"]["models"][0]["checks"]
    assert check["outcome"] == "error"
    attempt, = check["attempts"]
    assert attempt["exit_code"] == 2 and attempt["proxy_exchanges"] == []
    assert (attempt["outcome"], attempt["reason"], attempt["attribution"]) == (
        "error", "harness_cli_rejected", "canary",
    )
    assert "--run-budget" not in attempt["argv"]
    assert main(["report"]) == 0
    assert "harness_cli_rejected" in Path("report/index.html").read_text(encoding="utf-8")
    assert not list(Path("repro").glob("*.md"))
