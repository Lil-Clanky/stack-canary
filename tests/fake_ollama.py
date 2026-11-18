"""Deterministic loopback fake for the Ollama endpoints used by Layer A."""

import argparse
import hashlib
import json
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread

MODES = (
    "ok", "hang_before_headers", "stall_mid_stream", "slow_drip", "no_done",
    "single_chunk", "leak_tool_markup", "bad_tool_args", "split_tool_args", "rewrite_code",
    "http_500", "flaky", "cpu_fallback", "pull_error",
)
MODELS = ("a", "b", *(f"fake:{mode}" for mode in MODES))


class FakeOllama(ThreadingHTTPServer):
    def __init__(self, port: int = 0, mode: str = "ok", *,
                 version: str = "0.0.0-fake", models_dir: Path | None = None,
                 requires: str = "0.0.0"):
        if port == 11434:
            raise ValueError("port 11434 is reserved")
        if mode not in MODES:
            raise ValueError(f"unknown fixture mode: {mode}")
        self.mode = mode
        self.version = version
        self.models_dir = models_dir
        self.requires = requires
        self.loaded = None
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

    def tags(self):
        if self.models_dir is None:
            return [_tag(model) for model in MODELS]
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.models_dir.glob("*.json"))
        ]

    def save_model(self, model, source=""):
        tag = _tag(_model_key(model), source)
        name = hashlib.sha256(tag["name"].encode()).hexdigest()
        (self.models_dir / f"{name}.json").write_text(json.dumps(tag), encoding="utf-8")


def _model_key(name):
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def _tag(model, source=""):
    digest = hashlib.sha256((model + source).encode()).hexdigest()
    return {"name": model, "model": model, "digest": "sha256:" + digest}


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
            self._json({"version": self.server.version})
        elif self.path == "/api/tags":
            self._json({"models": self.server.tags()})
        elif self.path == "/api/ps":
            models = []
            if self.server.loaded:
                models.append({
                    **self.server.loaded, "size": 1024, "runner": "fake-runner",
                    "size_vram": 0 if self.server.mode == "cpu_fallback" else 768,
                })
            self._json({"models": models})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if not isinstance(body, dict) or not isinstance(body.get("model"), str):
                raise ValueError
        except (ValueError, RecursionError):
            self._json({"error": "expected a JSON request with a model"}, 400)
            return

        model = body["model"]
        if self.server.models_dir is not None:
            tags = {tag["name"]: tag for tag in self.server.tags()}
            if self.path == "/api/pull":
                self._pull(model)
                return
            if self.path == "/api/create":
                source = tags.get(_model_key(body.get("from", "")))
                if source is None or body.get("parameters") != {"temperature": 0, "seed": 42}:
                    self._json({"error": "invalid derived-model request"}, 400)
                    return
                self.server.save_model(model, source["digest"])
                self._json({"status": "success"})
                return
            if _model_key(model) not in tags:
                self._json({"error": "unknown model"}, 404)
                return
            if self.path == "/api/show":
                self._json({
                    "capabilities": (["decision"] if _model_key(model) == "decision-only:latest"
                                     else ["completion", "tools", "thinking"]),
                    "requires": self.server.requires,
                    "model_info": {"general.architecture": "fake", "fake.context_length": 131072},
                })
                return
            if self.path == "/v1/chat/completions":
                self.server.loaded = tags[_model_key(model)]
                if body.get("max_tokens") == 1:
                    if _model_key(model) == "canary-warmup-error:latest":
                        self._json({"error": "model cannot complete: " + "x" * 3000}, 400)
                        return
                    if _model_key(model) == "canary-server-dead:latest":
                        os._exit(7)
                    print(f"inference compute: Metal offloaded {_model_key(model)}", flush=True)
        if self.path != "/v1/chat/completions":
            self._json({"error": "not found"}, 404)
            return

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

    def _pull(self, model):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            if self.server.mode == "pull_error":
                self._chunk(b'{"error":"fake pull error"}\n')
            else:
                self._chunk(b'{"status":"pulling",')
                self._chunk(b'"total":128,"completed":0}\n')
                self.server.save_model(model)
                print(f"pulled {model}", flush=True)
                self._chunk(b'{"status":"success"}\n')
            self.wfile.write(b"0\r\n\r\n")
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
        name = body["tools"][0]["function"]["name"]
        code = 'type Box<T name="any"> = { value: T };' if mode == "rewrite_code" else "type Box<T=any> = { value: T };"
        arguments = ('{invalid' if mode == "bad_tool_args" else
                     json.dumps({"path": "box.ts", "content": code}) if name == "write_file" else '{"city":"Amsterdam"}')
        message["content"] = None
        message["tool_calls"] = [{
            "id": "call_fake_1",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
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
    parser.add_argument("command", choices=("serve",), nargs="?")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--mode", choices=MODES, default="ok")
    args = parser.parse_args()
    if args.command == "serve":
        host, separator, port = os.environ["OLLAMA_HOST"].rpartition(":")
        if host != "127.0.0.1" or not separator:
            parser.error("serve requires OLLAMA_HOST=127.0.0.1:PORT")
        args.port = int(port)
        args.mode = os.environ.get("FAKE_OLLAMA_MODE", args.mode)
        options = {
            "version": os.environ["FAKE_OLLAMA_VERSION"],
            "models_dir": Path(os.environ["OLLAMA_MODELS"]),
            "requires": os.environ.get("FAKE_OLLAMA_REQUIRES", "0.0.0"),
        }
    else:
        options = {}
    if not 0 <= args.port <= 65535 or args.port == 11434:
        parser.error("use a port from 0 to 65535 other than 11434")
    with FakeOllama(args.port, args.mode, **options) as server:
        print(server.endpoint, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
