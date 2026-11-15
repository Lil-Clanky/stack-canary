"""Deterministic loopback fake for the Ollama endpoints used by Layer A."""

import argparse
import hashlib
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread

MODES = (
    "ok", "hang_before_headers", "stall_mid_stream", "slow_drip", "no_done",
    "single_chunk", "leak_tool_markup", "bad_tool_args", "split_tool_args",
    "http_500", "flaky",
)
MODELS = ("a", "b", *(f"fake:{mode}" for mode in MODES))


class FakeOllama(ThreadingHTTPServer):
    def __init__(self, port: int = 0, mode: str = "ok"):
        if port == 11434:
            raise ValueError("port 11434 is reserved")
        if mode not in MODES:
            raise ValueError(f"unknown fixture mode: {mode}")
        self.mode = mode
        self.stopping = Event()
        self.lock = Lock()
        self.chat_requests = 0
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def _json(self, value: dict, status: int = 200) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            self._json({"version": "0.0.0-fake"})
        elif self.path == "/api/tags":
            self._json({"models": [
                {"name": model, "model": model,
                 "digest": "sha256:" + hashlib.sha256(model.encode()).hexdigest()}
                for model in MODELS
            ]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json({"error": "not found"}, 404)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if not isinstance(body, dict) or not isinstance(body.get("model"), str):
                raise ValueError
        except (ValueError, RecursionError):
            self._json({"error": "expected a JSON request with a model"}, 400)
            return

        model = body["model"]
        model_mode = model.removeprefix("fake:")
        mode = model_mode if model.startswith("fake:") and model_mode in MODES else self.server.mode
        with self.server.lock:
            self.server.chat_requests += 1
            even_request = self.server.chat_requests % 2 == 0
        try:
            if mode == "hang_before_headers":
                self.server.stopping.wait()
            elif mode == "http_500" or (mode == "flaky" and even_request):
                self._json({"error": "injected failure"}, 500)
            elif body.get("stream"):
                self._stream(body, mode)
            else:
                message, finish = _reply(body, mode)
                self._json({
                    "id": "chatcmpl-fake", "object": "chat.completion", "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                })
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _stream(self, body: dict, mode: str) -> None:
        message, finish = _reply(body, mode)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if mode == "slow_drip":
            while not self.server.stopping.is_set():
                self._chunk(b"d")
                if self.server.stopping.wait(1):
                    break
            return

        for index, (delta, reason) in enumerate(_deltas(message, finish, mode)):
            chunk = {
                "id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
            }
            self._chunk(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
            if mode == "stall_mid_stream" and index == 0:
                self.server.stopping.wait()
                return
        if mode != "no_done":
            self._chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")


def _reply(body: dict, mode: str) -> tuple[dict, str]:
    message = {"role": "assistant", "content": "CANARY"}
    if any(isinstance(entry, dict) and entry.get("role") == "tool"
           for entry in body.get("messages", [])):
        message["content"] = "It is 17 °C with drizzle in Amsterdam."
    elif body.get("tools"):
        if mode == "leak_tool_markup":
            message["content"] = '<tool_call>{"name":"get_weather"}</tool_call>'
            return message, "stop"
        arguments = '{invalid' if mode == "bad_tool_args" else '{"city":"Amsterdam"}'
        message["content"] = None
        message["tool_calls"] = [{
            "id": "call_fake_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": arguments},
        }]
        return message, "tool_calls"
    return message, "stop"


def _deltas(message: dict, finish: str, mode: str):
    if "tool_calls" in message:
        call = message["tool_calls"][0]
        function = call["function"]
        arguments = function["arguments"]
        parts = [arguments]
        if mode == "split_tool_args":
            parts = [arguments[:5], arguments[5:12], arguments[12:]]
        yield {"role": "assistant", "tool_calls": [{
            "index": 0, **call, "function": {"name": function["name"], "arguments": parts[0]},
        }]}, None
        for part in parts[1:]:
            yield {"tool_calls": [{"index": 0, "function": {"arguments": part}}]}, None
        yield {}, finish
        return

    content = message["content"]
    if mode == "single_chunk":
        yield {"role": "assistant", "content": content}, finish
        return
    midpoint = len(content) // 2
    yield {"role": "assistant", "content": content[:midpoint]}, None
    yield {"content": content[midpoint:]}, None
    yield {}, finish


@contextmanager
def running(mode: str = "ok"):
    with FakeOllama(mode=mode) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.stopping.set()
            server.shutdown()
            thread.join()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="ok")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535 or args.port == 11434:
        parser.error("use a port from 0 to 65535 other than 11434")
    with FakeOllama(args.port, args.mode) as server:
        print(server.endpoint, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
