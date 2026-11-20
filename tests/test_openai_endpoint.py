import json
from pathlib import Path

import pytest

import fake_openai_endpoint as fake
from fake_ollama import running as running_ollama
from stack_canary.cli import main
from stack_canary.config import Settings
from test_runtime_ollama import home  # noqa: F401  (fixture: temporary CANARY_HOME)


@pytest.fixture(autouse=True)
def short_timeouts(monkeypatch, tmp_path):
    from stack_canary import config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "load", lambda path=Path("canary.toml"): Settings(deadline_s=2, stall_s=0.5))


def _result():
    [path] = Path("results").glob("*_endpoint.json")
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_models_come_from_v1_models_and_run_once_by_default(capsys):
    with fake.running() as server:
        assert main(["run", "--openai-endpoint", server.endpoint]) == 0
        assert server.chat_requests == 12  # two models, A1 to A6, one attempt each
        endpoint = server.endpoint
    path, result = _result()
    assert result["endpoint"] == endpoint and result["endpoint_kind"] == "openai"
    assert [model["name"] for model in result["models"]] == ["m1", "m2"]
    assert all(len(check["attempts"]) == 1 for model in result["models"] for check in model["checks"])
    assert result["settings"]["repeats"] == 1
    assert capsys.readouterr().out.splitlines()[0].split() == ["MODEL", "A1", "A2", "A3", "A4", "A5", "A6"]


def test_model_flag_skips_discovery_and_attempts_flag_applies():
    with fake.running() as server:
        args = ["run", "--openai-endpoint", server.endpoint, "--model", "only", "--checks", "A1", "--attempts", "2"]
        assert main(args) == 0
        assert server.chat_requests == 2
    _, result = _result()
    assert [model["name"] for model in result["models"]] == ["only"]


def test_api_key_is_sent_and_never_written(monkeypatch, capsys):
    monkeypatch.setenv("FIXTURE_KEY", fake.KEY)
    summary = Path("summary.json")
    with fake.running() as server:
        args = ["run", "--openai-endpoint", server.endpoint, "--api-key-env", "FIXTURE_KEY",
                "--checks", "A1,A5", "--summary-json", str(summary)]
        assert main(args) == 0
        assert server.auth_headers and set(server.auth_headers) == {"Bearer " + fake.KEY}
    path, result = _result()
    output = capsys.readouterr()
    recorded = [attempt["request"]["headers"] for model in result["models"]
                for check in model["checks"] for attempt in check["attempts"]]
    assert recorded and all("Authorization" not in headers for headers in recorded)
    for text in (path.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8"), output.out, output.err):
        assert fake.KEY not in text


def test_key_with_control_characters_is_refused_by_name(monkeypatch, capsys):
    monkeypatch.setenv("FIXTURE_KEY", "sk-bad\nkey")
    with fake.running() as server:
        assert main(["run", "--openai-endpoint", server.endpoint, "--api-key-env", "FIXTURE_KEY"]) == 2
        assert server.auth_headers == []
    error = capsys.readouterr().err
    assert "FIXTURE_KEY" in error and "control characters" in error and "sk-bad" not in error


def test_unwritable_summary_names_the_file_and_leaves_no_temporary(capsys):
    with fake.running() as server:
        assert main(["run", "--openai-endpoint", server.endpoint, "--model", "m1", "--checks", "A1",
                     "--summary-json", "missing/dir/summary.json"]) == 2
    error = capsys.readouterr().err
    assert "could not write summary missing/dir/summary.json" in error and "No such file" in error
    assert not Path("missing").exists()


def test_unset_api_key_variable_is_refused(monkeypatch, capsys):
    monkeypatch.delenv("FIXTURE_KEY", raising=False)
    with fake.running() as server:
        assert main(["run", "--openai-endpoint", server.endpoint, "--api-key-env", "FIXTURE_KEY"]) == 2
        assert server.auth_headers == []
    assert "FIXTURE_KEY" in capsys.readouterr().err and not Path("results").exists()


@pytest.mark.parametrize("url", ["http://example.com:3001/v1", "http://127.0.0.1:3001", "http://127.0.0.1:3001/v1/x",
                                 "http://127.0.0.1:3001/v1?a=b", "https://127.0.0.1:3001/v1"])
def test_non_loopback_or_wrong_path_is_refused(url, capsys):
    assert main(["run", "--openai-endpoint", url, "--model", "m"]) == 2
    assert capsys.readouterr().err.startswith("canary:") and not Path("results").exists()


def test_summary_json_layout_for_openai_and_ollama_endpoints():
    with fake.running() as server:
        assert main(["run", "--openai-endpoint", server.endpoint, "--checks", "A1,A3", "--model", "m1",
                     "--summary-json", "openai.json"]) == 0
        endpoint = server.endpoint
    assert json.loads(Path("openai.json").read_text(encoding="utf-8")) == {
        "endpoint": endpoint, "endpoint_kind": "openai",
        "results": [{"model": "m1", "check": check, "status": "pass", "passed": 1, "attempts": 1}
                    for check in ("A1", "A3")]}
    with running_ollama() as server:
        assert main(["run", "--endpoint", server.endpoint, "--models", "a", "--checks", "A1",
                     "--repeats", "2", "--summary-json", "ollama.json"]) == 0
        endpoint = server.endpoint
    summary = json.loads(Path("ollama.json").read_text(encoding="utf-8"))
    assert summary["endpoint"] == endpoint and summary["endpoint_kind"] == "ollama"
    assert summary["results"][0] == {"model": "a", "check": "A1", "status": "pass", "passed": 2, "attempts": 2}


def test_failing_check_status_in_summary():
    with running_ollama("http_500") as server:
        assert main(["run", "--endpoint", server.endpoint, "--models", "a", "--checks", "A1",
                     "--repeats", "1", "--summary-json", "s.json"]) == 1
    [row] = json.loads(Path("s.json").read_text(encoding="utf-8"))["results"]
    assert row["status"] == "fail" and row["passed"] == 0


def test_ollama_endpoint_results_record_their_kind():
    with running_ollama() as server:
        assert main(["run", "--endpoint", server.endpoint, "--models", "a", "--checks", "A1", "--repeats", "1"]) == 0
    assert _result()[1]["endpoint_kind"] == "ollama"


def test_openai_results_never_enter_matrix_json(home):
    from stack_canary.report import generate

    with fake.running() as server:
        assert main(["run", "--openai-endpoint", server.endpoint, "--model", "m1", "--checks", "A1"]) == 0
    # Without a runtime version the report loader skips the result, so there is nothing to report.
    with pytest.raises(ValueError, match="no Stack Canary results"):
        generate()
