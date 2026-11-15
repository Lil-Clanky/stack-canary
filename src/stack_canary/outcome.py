"""Aggregate repeated check attempts."""

from collections.abc import Iterable
from typing import Literal

AttemptOutcome = Literal["pass", "fail", "timeout"]
CheckOutcome = Literal["pass", "fail", "timeout", "flaky"]


def aggregate(attempts: Iterable[AttemptOutcome]) -> CheckOutcome:
    outcomes = set(attempts)
    if not outcomes or not outcomes <= {"pass", "fail", "timeout"}:
        raise ValueError("expected at least one valid attempt outcome")
    if outcomes == {"pass"}:
        return "pass"
    if outcomes == {"timeout"}:
        return "timeout"
    if "pass" in outcomes:
        return "flaky"
    return "fail"
