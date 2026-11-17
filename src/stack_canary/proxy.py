"""Record an isolated Hermes attempt's HTTP traffic to managed Ollama."""

import http.client
import socket
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .transport import loopback_url, parse_json

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


def _completion(method, target):
    return method == "POST" and target.partition("?")[0] == "/v1/chat/completions"


def broken_completion(exchange):
    request = exchange.get("request") or {}
    if not _completion(request.get("method"), request.get("path") or ""):
        return False
    status = (exchange.get("response") or {}).get("status")
    return (isinstance(status, int) and not 200 <= status < 300
            or exchange.get("error") in {"bad_json", "bad_sse", "no_done"}
            or isinstance(status, int) and exchange.get("error") == "upstream_error")


def _forwarded(headers):
    blocked = HOP_HEADERS | {
        value.strip().lower()
        for name, values in headers if name.lower() == "connection"
        for value in values.split(",")
    }
    return [(name, value) for name, value in headers if name.lower() not in blocked]


def _text(data):
    return data.decode("utf-8", errors="surrogateescape")


class RecordingProxy(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, upstream: str, *, timeout=30):
        self.upstream_port, target = loopback_url(upstream)
        if (target != "/" or "?" in upstream or "#" in upstream
                or self.upstream_port in {9119, 9120}):
            raise ValueError("the proxy requires a dedicated managed Ollama base URL")
        self.timeout = timeout
        self.exchanges = []
        self._lock = threading.Lock()
        self._sockets = set()
        self._closing = False
        super().__init__(("127.0.0.1", 0), _Handler)

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(self.timeout)
        self._track(sock)
        return sock, address

    def shutdown_request(self, request):
        self._forget(request)
        super().shutdown_request(request)

    def _track(self, sock):
        with self._lock:
            if self._closing:
                sock.shutdown(socket.SHUT_RDWR)
            self._sockets.add(sock)

    def _forget(self, sock):
        with self._lock:
            self._sockets.discard(sock)

    def _record(self, exchange):
        with self._lock:
            self.exchanges.append(exchange)

    def __enter__(self):
        self._thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05})
        self._thread.start()
        return self

    def __exit__(self, *_):
        self.shutdown()
        with self._lock:
            self._closing = True
            for sock in self._sockets:
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
        self.server_close()
        self._thread.join()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _handle(self):
        target = self.path
        if (not target.startswith("/") or target.startswith("//") or target == "/"
                or any(x in target for x in ("\r", "\n", " ", "#", "\\"))):
            self.send_error(400, "origin-form Ollama request required")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 <= length <= 16 * 1024 ** 2:
                raise ValueError
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
        except (ValueError, OSError):
            self.send_error(400, "invalid proxy request body")
            return
        self.close_connection = True
        headers = list(self.headers.items())
        request = {
            "method": self.command, "path": target, "body": _text(body) if body else None,
            "headers": [(k, "[redacted]" if k.lower() in {"authorization", "cookie"} else v)
                        for k, v in headers],
        }
        capture = {"status": None, "headers": [], "body": None, "lines": None}
        exchange = {"request": request, "response": capture, "error": None}
        started = time.monotonic()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port,
                                                timeout=self.server.timeout)
        upstream_socket = None
        try:
            connection.connect()
            upstream_socket = connection.sock
            self.server._track(upstream_socket)
            outgoing = {k: v for k, v in _forwarded(headers)
                        if k.lower() not in {"host", "accept-encoding", "authorization", "cookie"}}
            outgoing.update({"Accept-Encoding": "identity", "Connection": "close"})
            connection.request(self.command, target, body=body if body else None, headers=outgoing)
            with connection.getresponse() as response:
                capture["status"] = response.status
                capture["headers"] = response.getheaders()
                self._relay(response, capture, exchange, started)
        except (OSError, http.client.HTTPException) as error:
            if self.server._closing:
                self._client_disconnected(exchange, error)
            else:
                exchange["error"] = exchange["error"] or "upstream_error"
                exchange["upstream_error"] = {"type": type(error).__name__, "message": str(error)}
            if capture["status"] is None and not self.server._closing:
                try:
                    self.send_error(502, "managed Ollama did not answer")
                except OSError as disconnect:
                    self._client_disconnected(exchange, disconnect)
        finally:
            connection.close()
            if upstream_socket is not None:
                self.server._forget(upstream_socket)
            exchange["elapsed_s"] = time.monotonic() - started
            self.server._record(exchange)

    def _relay(self, response, capture, exchange, started):
        sse = response.getheader("Content-Type", "").lower().startswith("text/event-stream")
        no_body = self.command == "HEAD" or response.status in {204, 304}
        completion = _completion(self.command, self.path)
        if not 200 <= response.status < 300:
            exchange["error"] = "http_error"
        if sse:
            capture["lines"] = []
        pending = bytearray()
        done = False

        def sse_line(raw):
            nonlocal done
            capture["lines"].append({"elapsed_s": time.monotonic() - started, "line": _text(raw)})
            if not raw.startswith(b"data:"):
                return
            data = raw[5:].strip()
            if data == b"[DONE]":
                done = True
            else:
                try:
                    if not isinstance(parse_json(data), dict) and completion:
                        raise ValueError("expected a JSON object")
                except (ValueError, RecursionError):
                    exchange["error"] = exchange["error"] or "bad_sse"

        # Relay a close-delimited body unless the upstream supplied a length.
        try:
            self.send_response_only(response.status, response.reason)
            for name, value in _forwarded(capture["headers"]):
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
        except OSError as error:
            self._client_disconnected(exchange, error)
            return

        try:
            while not no_body and (data := response.read1(8192)):
                pending.extend(data)
                if sse:
                    while (end := pending.find(b"\n")) >= 0:
                        sse_line(bytes(pending[:end + 1]))
                        del pending[:end + 1]
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except OSError as error:
                    self._client_disconnected(exchange, error)
                    return
            if response.length and not no_body:
                raise http.client.IncompleteRead(b"", response.length)
        finally:
            if not sse:
                capture["body"] = _text(pending)
        if self.server._closing and response.length is None and not done:
            self._client_disconnected(exchange, ConnectionAbortedError("proxy stopped while reading the body"))
            return
        if sse:
            if pending:
                sse_line(bytes(pending))
            if not done:
                exchange["error"] = exchange["error"] or "no_done"
        elif (not no_body or completion) and exchange["error"] is None:
            try:
                if not isinstance(parse_json(bytes(pending)), dict) and completion:
                    raise ValueError("expected a JSON object")
            except (ValueError, RecursionError):
                exchange["error"] = "bad_json"

    @staticmethod
    def _client_disconnected(exchange, error):
        exchange["error"] = exchange["error"] or "client_disconnect"
        exchange["client_disconnect"] = {"type": type(error).__name__, "message": str(error)}

    do_GET = do_POST = do_HEAD = do_OPTIONS = do_PUT = do_DELETE = do_PATCH = _handle
