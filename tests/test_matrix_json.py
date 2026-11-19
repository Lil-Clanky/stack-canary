import json
from pathlib import Path

import pytest

from stack_canary import publish
from stack_canary.config import Settings
from stack_canary.report import generate
from test_report import _check, _record
from test_runtime_ollama import home  # noqa: F401  (fixture)

PASS = {"outcome": "pass"}
FAIL = {"outcome": "fail", "reason": "no_tool_call"}
MODEL = "qwen3.5:0.8b"


def _seed(directory):
    _record(directory, "20261114T100000Z", "0.40.15", [
        _check("A1", "pass", attempts=(PASS,) * 3),
        _check("A6", "pass", attempts=(PASS,) * 3),
    ], model_name=MODEL)
    _record(directory, "20261115T100000Z", "0.40.16", [
        _check("A1", "pass", attempts=(PASS,) * 3),
        _check("A3", "flaky", attempts=(PASS, FAIL, FAIL)),
        _check("A4", "timeout", attempts=({"outcome": "timeout"},) * 3),
        _check("A5", "n/a", reason="model_lacks_tools"),
        _check("A6", "fail", attempts=(FAIL,) * 3),
        _check("B6", "fail", attempts=(FAIL,)),
    ], model_name=MODEL)


def _cells(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 1 and data["generated"].startswith("2026-11-15T10:00:00")
    return {(c["ollama"], c["model"], c["check"]): c for c in data["cells"]}


def test_matrix_json_cells(home):
    _seed(Path("results"))
    generate()
    cells = _cells(Path("report/matrix.json"))
    assert cells["0.40.16", MODEL, "A6"] == {
        "ollama": "0.40.16", "model": MODEL, "check": "A6",
        "status": "fail", "passed": 0, "attempts": 3}
    assert cells["0.40.15", MODEL, "A6"]["status"] == "pass"
    assert cells["0.40.16", MODEL, "A3"]["status"] == "fail"  # flaky is not a pass
    assert cells["0.40.16", MODEL, "A3"]["passed"] == 1
    assert cells["0.40.16", MODEL, "A4"]["status"] == "fail"  # timeout is not a pass
    assert cells["0.40.16", MODEL, "A5"] == {
        "ollama": "0.40.16", "model": MODEL, "check": "A5",
        "status": "n/a", "passed": 0, "attempts": 0}
    assert not any(check == "B6" for _, _, check in cells)


def test_matrix_json_leaves_out_openai_endpoint_results(home):
    _seed(Path("results"))
    _record(Path("results"), "20261116T100000Z", "0.99.0", [_check("A1", "pass", attempts=(PASS,))],
            model_name="gateway-model", endpoint_kind="openai", endpoint="http://127.0.0.1:3001/v1")
    generate()
    data = json.loads(Path("report/matrix.json").read_text(encoding="utf-8"))
    assert {cell["model"] for cell in data["cells"]} == {MODEL}


def test_publish_lists_and_sanitizes_matrix_json():
    assert "matrix.json" in publish.ARTIFACTS
    settings = Settings(ntfy_topic="fixture-topic")
    assert publish._sanitize('{"model": "/Users/person/m"}', settings, "tok") == '{"model": "~/m"}'
    with pytest.raises(ValueError, match="credential-like"):
        publish._sanitize('{"model": "fixture-topic"}', settings, "tok")
