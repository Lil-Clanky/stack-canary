"""Loopback fake of a bare OpenAI-compatible server: /v1/models and /v1/chat/completions only."""

import socketserver
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from threading import Thread

from fake_ollama import Handler as OllamaHandler

KEY = "sk-fixture-secret-value-0123456789"


class Handler(OllamaHandler):
    def do_GET(self):
        self.server.auth_headers.append(self.headers.get("Authorization"))
        if self.path == "/v1/models":
            self._json({"object": "list", "data": [{"id": m, "object": "model"} for m in self.server.models]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        self.server.auth_headers.append(self.headers.get("Authorization"))
        super().do_POST()


class FakeOpenAI(ThreadingHTTPServer):
    def __init__(self, models=("m1", "m2")):
        import fake_ollama

        self.models = list(models)
        self.auth_headers = []
        self.mode = "ok"
        self.models_dir = None
        self.stopping = fake_ollama.Event()
        self.lock = fake_ollama.Lock()
        self.chat_requests = 0
        super().__init__(("127.0.0.1", 0), Handler)

    def server_bind(self):
        # HTTPServer.server_bind does a reverse DNS lookup that stalls on macOS CI.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.server_port}/v1"


@contextmanager
def running(**options):
    with FakeOpenAI(**options) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.stopping.set()
            server.shutdown()
            thread.join()
