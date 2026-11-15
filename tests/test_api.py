import json

import pytest

import fake_ollama
from fake_ollama import running
from stack_canary.checks.api import run_attempt
from stack_canary.outcome import aggregate


def attempt(check_id, endpoint, model="a", number=1, *, stall_s=0.3, deadline_s=2, thinking=False):
    return run_attempt(
        check_id, endpoint, model, number,
        stall_s=stall_s, deadline_s=deadline_s, thinking=thinking,
    )


@pytest.mark.parametrize("check_id", ["A1", "A2", "A3", "A4", "A5"])
def test_ok_and_request_capture(check_id):
    with running() as server:
        result = attempt(check_id, server.endpoint, thinking=check_id == "A4")
        assert result["request"]["url"] == server.endpoint + "/v1/chat/completions"
    assert (result["check_id"], result["attempt"], result["outcome"], result["reason"]) == (
        check_id, 1, "pass", None
    )

    request = result["request"]
    assert request["method"] == "POST"
    assert request["headers"]["Content-Type"] == "application/json"
    assert request["headers"]["Content-Length"] == str(len(request["body"].encode("utf-8")))
    body = json.loads(request["body"])
    assert body["model"] == "a"
    assert (body["temperature"], body["seed"], body["max_tokens"]) == (0, 42, 256)
    assert body["stream"] is (check_id in {"A2", "A4"})
    if check_id == "A4":
        assert body["reasoning_effort"] == "none"
    else:
        assert "reasoning_effort" not in body
    if check_id in {"A1", "A2"}:
        assert body["messages"] == [
            {"role": "system", "content": "You are a test fixture."},
            {"role": "user", "content": "Reply with exactly the word CANARY and nothing else."},
        ]
    else:
        assert body["messages"][0] == {
            "role": "user", "content": "What is the weather in Amsterdam right now? Use the get_weather tool."
        }
        tool = body["tools"][0]["function"]
        assert tool["name"] == "get_weather"
        assert tool["parameters"]["required"] == ["city"]
        assert tool["parameters"]["properties"]["city"]["type"] == "string"
    if check_id == "A5":
        call = body["messages"][1]["tool_calls"][0]
        tool_result = body["messages"][2]
        assert call["id"] == tool_result["tool_call_id"] == "call_canary_1"
        assert call["function"]["arguments"] == '{"city":"Amsterdam"}'
        assert json.loads(tool_result["content"]) == {
            "city": "Amsterdam", "temperature_c": 17, "condition": "drizzle"
        }

    response = result["response"]
    assert response["status"] == 200
    if body["stream"]:
        assert response["body"] is None
        assert dict(response["headers"])["Content-Type"] == "text/event-stream"
        assert any(line["line"] == "data: [DONE]\n" for line in response["lines"])
        stamps = [line["elapsed_s"] for line in response["lines"]]
        assert stamps == sorted(stamps)
        assert 0 <= stamps[0] <= stamps[-1] <= result["duration_s"]
    else:
        assert response["lines"] is None
        assert json.loads(response["body"])["choices"]


@pytest.mark.parametrize(
    ("mode", "check_id", "outcome", "reason"),
    [
        ("hang_before_headers", "A1", "timeout", "timeout_stall"),
        ("stall_mid_stream", "A2", "timeout", "timeout_stall"),
        ("no_done", "A2", "fail", "no_done"),
        ("no_done", "A4", "fail", "no_done"),
        ("single_chunk", "A2", "fail", "too_few_chunks"),
        ("leak_tool_markup", "A3", "fail", "tool_markup_leaked"),
        ("leak_tool_markup", "A4", "fail", "tool_markup_leaked"),
        ("bad_tool_args", "A3", "fail", "bad_tool_args"),
        ("bad_tool_args", "A4", "fail", "bad_tool_args"),
        ("split_tool_args", "A4", "pass", None),
        ("http_500", "A1", "fail", "http_error"),
        ("http_500", "A2", "fail", "http_error"),
    ],
)
def test_fixture_modes_by_model(mode, check_id, outcome, reason):
    with running() as server:
        result = attempt(check_id, server.endpoint, model=f"fake:{mode}")
    assert (result["outcome"], result["reason"]) == (outcome, reason)
    if mode == "stall_mid_stream":
        assert result["response"]["status"] == 200
        assert result["response"]["lines"][0]["line"].startswith("data: {")
    if mode == "hang_before_headers":
        assert result["response"]["status"] is None
    if mode == "http_500":
        assert result["response"]["status"] == 500


def test_slow_drip_respects_hard_deadline_and_preserves_partial_line():
    with running("slow_drip") as server:
        result = attempt("A2", server.endpoint, deadline_s=1.8, stall_s=1.4)
    assert (result["outcome"], result["reason"]) == ("timeout", "timeout_deadline")
    assert [line["line"] for line in result["response"]["lines"]] == ["dd"]
    assert 1.7 <= result["duration_s"] < 2.8


def test_flaky_every_second_request():
    with running("flaky") as server:
        results = [attempt("A1", server.endpoint, number=n) for n in range(1, 4)]
    outcomes = [result["outcome"] for result in results]
    assert outcomes == ["pass", "fail", "pass"]
    assert results[1]["reason"] == "http_error"
    assert aggregate(outcomes) == "flaky"


@pytest.mark.parametrize(
    ("check_id", "message", "finish", "expected"),
    [
        ("A1", {"content": "canary"}, "length", "wrong_finish_reason"),
        ("A2", {"content": "canary"}, "length", "wrong_finish_reason"),
        ("A1", {"content": "sparrow"}, "stop", "wrong_content"),
        ("A2", {"content": "sparrow"}, "stop", "wrong_content"),
        ("A3", {"content": None}, "stop", "no_tool_call"),
        ("A3", {"content": '[TOOL_CALLS]'}, "tool_calls", "tool_markup_leaked"),
        ("A3", {"content": '<|python_tag|>'}, "tool_calls", "tool_markup_leaked"),
        ("A3", {"content": '<|channel|>'}, "tool_calls", "tool_markup_leaked"),
        ("A3", {"content": '"name": "get_weather"'}, "tool_calls", "tool_markup_leaked"),
        ("A3", {"tool_calls": [{"function": {"name": "other"}}]}, "tool_calls", "wrong_tool_name"),
        ("A3", {"tool_calls": [{"function": {"name": "get_weather", "arguments": "[]"}}]},
         "tool_calls", "bad_tool_args"),
        ("A3", {"tool_calls": [{"function": {"name": "get_weather", "arguments": '{"city":"Oslo"}'}}]},
         "tool_calls", "bad_tool_args"),
        ("A3", {"tool_calls": [{"function": {"name": "get_weather", "arguments": '{"city":"AMSTERDAM"}'}}]},
         "stop", "wrong_finish_reason"),
        ("A5", {"content": "It is 18 °C"}, "stop", "wrong_content"),
        ("A5", {"content": "17", "tool_calls": [{"function": {"name": "get_weather"}}]},
         "tool_calls", "unexpected_tool_call"),
    ],
)
def test_additional_semantic_failures(monkeypatch, check_id, message, finish, expected):
    monkeypatch.setattr(fake_ollama, "_reply", lambda body, mode: (message, finish))
    with running() as server:
        result = attempt(check_id, server.endpoint)
    assert (result["outcome"], result["reason"]) == ("fail", expected)


def test_a4_assembles_calls_by_index(monkeypatch):
    def interleaved_calls(message, finish, mode):
        yield {"tool_calls": [
            {"index": 1, "function": {"name": "other", "arguments": "{}"}},
            {"index": 0, "function": {"name": "get_weather", "arguments": '{"ci'}},
        ]}, None
        yield {"tool_calls": [
            {"index": 0, "function": {"arguments": 'ty":"aMsterdam"}'}},
        ]}, None
        yield {}, "tool_calls"

    monkeypatch.setattr(fake_ollama, "_deltas", interleaved_calls)
    with running() as server:
        result = attempt("A4", server.endpoint)
    assert (result["outcome"], result["reason"]) == ("pass", None)
