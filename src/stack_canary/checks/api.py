"""Layer A checks for the OpenAI-compatible chat protocol."""

import re
from dataclasses import asdict

from ..transport import Exchange, parse_json, request
from . import CHECKS

CHAT_PROMPT = "Reply with exactly the word CANARY and nothing else."
WEATHER_PROMPT = "What is the weather in Amsterdam right now? Use the get_weather tool."
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}
TOOL_MARKUP = re.compile(
    r'<tool_call>|\[TOOL_CALLS\]|<\|python_tag\|>|<\|channel\|>|"name"\s*:\s*"get_weather"'
)


def _body(check_id: str, model: str, thinking: bool) -> dict:
    body = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "max_tokens": 256,
        "stream": check_id in {"A2", "A4"},
    }
    if thinking:
        body["reasoning_effort"] = "none"
    if check_id in {"A1", "A2"}:
        body["messages"] = [
            {"role": "system", "content": "You are a test fixture."},
            {"role": "user", "content": CHAT_PROMPT},
        ]
        return body
    body["tools"] = [WEATHER_TOOL]
    body["messages"] = [{"role": "user", "content": WEATHER_PROMPT}]
    if check_id == "A5":
        body["messages"].extend([
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_canary_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Amsterdam"}'},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_canary_1",
                "content": '{"city":"Amsterdam","temperature_c":17,"condition":"drizzle"}',
            },
        ])
    return body


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _first(values: object) -> dict:
    if isinstance(values, list) and values:
        return _object(values[0])
    return {}


def _choice(payload: object) -> dict:
    return _first(_object(payload).get("choices"))


def _tool_reason(calls: object, content: str, finish: object) -> str | None:
    if TOOL_MARKUP.search(content):
        return "tool_markup_leaked"
    call = _first(calls)
    if not call:
        return "no_tool_call"
    function = _object(call.get("function"))
    if function.get("name") != "get_weather":
        return "wrong_tool_name"
    try:
        arguments = parse_json(_text(function.get("arguments")))
    except (ValueError, RecursionError):
        return "bad_tool_args"
    if "amsterdam" not in _text(_object(arguments).get("city")).casefold():
        return "bad_tool_args"
    if finish != "tool_calls":
        return "wrong_finish_reason"
    return None


def _streamed_calls(deltas: list[dict]) -> list[dict]:
    calls: dict[int, dict] = {}
    invalid_indices = False
    for delta in deltas:
        fragments = delta.get("tool_calls")
        if not isinstance(fragments, list):
            continue
        for fragment in fragments:
            index = _object(fragment).get("index")
            if type(index) is not int or index < 0:
                invalid_indices = True
                continue
            function = _object(fragment.get("function"))
            assembled = calls.setdefault(index, {"name": "", "arguments": ""})
            assembled["name"] += _text(function.get("name"))
            assembled["arguments"] += _text(function.get("arguments"))
    if invalid_indices:
        return []
    return [{"function": calls[index]} for index in sorted(calls)]


def _reason(check_id: str, exchange: Exchange) -> str | None:
    if exchange.error:
        return exchange.error
    if check_id in {"A2", "A4"}:
        if not exchange.done:
            return "no_done"
        choices = [_choice(chunk) for chunk in exchange.chunks]
        deltas = [_object(choice.get("delta")) for choice in choices]
        content = "".join(_text(delta.get("content")) for delta in deltas)
        finishes = [choice.get("finish_reason") for choice in choices]
        if check_id == "A4":
            finish = next((value for value in reversed(finishes) if value is not None), None)
            return _tool_reason(_streamed_calls(deltas), content, finish)
        if len(exchange.chunks) < 2:
            return "too_few_chunks"
        if "stop" not in finishes:
            return "wrong_finish_reason"
        return None if "canary" in content.casefold() else "wrong_content"

    choice = _choice(exchange.payload)
    message = _object(choice.get("message"))
    content = _text(message.get("content"))
    if check_id == "A3":
        return _tool_reason(message.get("tool_calls"), content, choice.get("finish_reason"))
    if check_id == "A5":
        if message.get("tool_calls"):
            return "unexpected_tool_call"
        return None if "17" in content else "wrong_content"
    if choice.get("finish_reason") != "stop":
        return "wrong_finish_reason"
    return None if "canary" in content.casefold() else "wrong_content"


def run_attempt(
    check_id: str,
    endpoint: str,
    model: str,
    attempt: int,
    *,
    deadline_s: float,
    stall_s: float,
    thinking: bool = False,
) -> dict:
    if check_id not in CHECKS:
        raise ValueError(f"unknown check {check_id}")
    body = _body(check_id, model, thinking)
    exchange = request(
        "POST",
        endpoint.rstrip("/") + "/v1/chat/completions",
        body,
        deadline_s=deadline_s,
        stall_s=stall_s,
        sse=body["stream"],
    )
    reason = _reason(check_id, exchange)
    if reason is None:
        outcome = "pass"
    elif reason in {"timeout_deadline", "timeout_stall"}:
        outcome = "timeout"
    else:
        outcome = "fail"
    return {
        "check_id": check_id,
        "attempt": attempt,
        "outcome": outcome,
        "reason": reason,
        "duration_s": exchange.elapsed_s,
        "request": exchange.request,
        "response": asdict(exchange.response),
    }
