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
    ],
)
def test_aggregate(attempts, expected):
    assert aggregate(attempts) == expected


@pytest.mark.parametrize("attempts", [[], ["unknown"]])
def test_invalid_attempts(attempts):
    with pytest.raises(ValueError):
        aggregate(attempts)


def test_report_stub(capsys):
    assert main(["report"]) == 0
    assert capsys.readouterr().out == "not implemented yet\n"
