import pytest

from stack_canary.cli import main
from stack_canary.outcome import aggregate


@pytest.mark.parametrize(
    ("attempts", "expected"),
    [
        (["pass", "pass", "pass"], "pass"),
        (["timeout", "timeout", "timeout"], "timeout"),
        (["pass", "fail", "pass"], "flaky"),
        (["pass", "timeout", "timeout"], "flaky"),
        (["pass", "fail", "timeout"], "flaky"),
        (["fail", "fail", "fail"], "fail"),
        (["timeout", "fail", "timeout"], "fail"),
        (["error", "fail", "pass"], "error"),
    ],
)
def test_aggregate(attempts, expected):
    assert aggregate(attempts) == expected


@pytest.mark.parametrize("attempts", [[], ["unknown"]])
def test_invalid_attempts(attempts):
    with pytest.raises(ValueError):
        aggregate(attempts)


def test_report_without_results(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CANARY_HOME", str(tmp_path / "canary-home"))
    assert main(["report"]) == 2
    assert "no Stack Canary results found" in capsys.readouterr().err
