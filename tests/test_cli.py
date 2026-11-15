import json
from pathlib import Path

import pytest

import fake_ollama
from fake_ollama import FakeOllama, running
from stack_canary import __version__
from stack_canary.cli import main


def test_run_defaults_and_result_schema(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    with running() as server:
        assert main(["run", "--endpoint", server.endpoint, "--models", "a,b"]) == 0
        endpoint = server.endpoint
        assert server.chat_requests == 30

    output = capsys.readouterr().out
    assert output.splitlines()[0].split() == ["MODEL", "A1", "A2", "A3", "A4", "A5"]
    assert output.splitlines()[1].split() == ["a", *["pass"] * 5]
    assert output.splitlines()[2].split() == ["b", *["pass"] * 5]
    [path] = Path("results").glob("*Z_endpoint.json")
    assert str(path) in output
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["schema_version"] == 1
    assert result["canary_version"] == __version__
    assert result["host"]["platform"] and result["host"]["python_version"]
    assert result["endpoint"] == endpoint
    assert result["runtime_version"] == "0.0.0-fake"
    assert result["settings"]["repeats"] == 3
    assert result["settings"]["checks"] == ["A1", "A2", "A3", "A4", "A5"]
    for model in result["models"]:
        assert model["digest"].startswith("sha256:")
        for check in model["checks"]:
            assert check["outcome"] == "pass"
            assert [attempt["attempt"] for attempt in check["attempts"]] == [1, 2, 3]
            assert all(attempt["check_id"] == check["check_id"] for attempt in check["attempts"])


def test_config_and_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("canary.toml").write_text(
        'repeats = 1\nresults_dir = "configured"\ndeadline_s = 1.5\nstall_s = 0.5\n'
        'thinking_models = ["fake:flaky"]\n',
        encoding="utf-8",
    )
    with running() as server:
        args = ["run", "--endpoint", server.endpoint, "--models", "fake:flaky", "--checks", "A1"]
        assert main([*args, "--repeats", "3", "--results-dir", "override"]) == 1
    [path] = Path("override").glob("*_endpoint.json")
    model = json.loads(path.read_text(encoding="utf-8"))["models"][0]
    assert model["thinking"]
    [check] = model["checks"]
    assert check["outcome"] == "flaky"
    assert all(json.loads(attempt["request"]["body"])["reasoning_effort"] == "none"
               for attempt in check["attempts"])
    assert not Path("configured").exists()


def test_invalid_config_is_infrastructure_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("canary.toml").write_text("stall_s = inf", encoding="utf-8")
    with running() as server:
        assert main(["run", "--endpoint", server.endpoint, "--models", "a"]) == 2
    assert "stall_s" in capsys.readouterr().err
    assert not Path("results").exists()


def test_reserved_port_refused(capsys):
    assert main(["run", "--endpoint", "http://127.0.0.1:11434", "--models", "a"]) == 2
    assert "11434" in capsys.readouterr().err
    with pytest.raises(ValueError, match="11434"):
        FakeOllama(port=11434)


def test_invalid_base_url_without_writing_results(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    with running() as server:
        assert main(["run", "--endpoint", server.endpoint + "/v1", "--models", "a"]) == 2
    assert "base URL" in capsys.readouterr().err
    assert not Path("results").exists()


def test_preflight_fails_without_writing_results(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    def unavailable(handler):
        handler.close_connection = True

    monkeypatch.setattr(fake_ollama.Handler, "do_GET", unavailable)
    with running() as server:
        assert main(["run", "--endpoint", server.endpoint, "--models", "a"]) == 2
        assert server.chat_requests == 0
    assert "/api/version: connection_error" in capsys.readouterr().err
    assert not Path("results").exists()


def test_results_write_error_is_infrastructure_error(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    Path("results").touch()
    with running() as server:
        args = ["run", "--endpoint", server.endpoint, "--models", "a", "--checks", "A1", "--repeats", "1"]
        assert main(args) == 2
    assert "could not write results" in capsys.readouterr().err
