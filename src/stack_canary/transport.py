"""HTTP transport with bounded reads and raw response capture."""

import http.client
import io
import json
import math
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def loopback_url(url: str) -> tuple[int, str]:
    """Return the port and request target of an allowed phase 2 URL."""
    if any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise ValueError("the endpoint URL cannot contain spaces or control characters")
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("use a plain HTTP endpoint on 127.0.0.1 or localhost")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError("the endpoint URL cannot contain credentials or a fragment")
    port = parts.port
    if not port or port == 11434:
        raise ValueError("use an explicit nonzero port other than 11434")
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    return port, target


class _DeadlineExpired(TimeoutError):
    pass


class _TimedReader(io.RawIOBase):
    """Apply the two clocks to every socket read, including within HTTP headers."""

    def __init__(self, sock: socket.socket, deadline: float, stall_s: float):
        self.sock = sock
        self.raw = sock.makefile("rb", buffering=0)
        self.deadline = deadline
        self.stall_s = stall_s
        self.last_byte = time.monotonic()

    def makefile(self, mode: str) -> io.BufferedReader:
        # HTTPResponse receives this wrapper in place of the socket.
        return io.BufferedReader(self)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        now = time.monotonic()
        remaining = self.deadline - now
        if remaining <= 0:
            raise _DeadlineExpired
        idle = self.last_byte + self.stall_s - now
        if idle <= 0:
            raise TimeoutError
        self.sock.settimeout(min(remaining, idle))
        try:
            count = self.raw.readinto(buffer)
        except TimeoutError:
            if remaining <= idle:
                raise _DeadlineExpired from None
            raise
        if count:
            self.last_byte = time.monotonic()
        return count

    def close(self) -> None:
        self.raw.close()
        super().close()


class _TimedConnection(http.client.HTTPConnection):
    def __init__(self, port: int, deadline: float, stall_s: float):
        self.deadline = deadline
        self.stall_s = stall_s
        super().__init__("127.0.0.1", port, timeout=self._timeout())

    def _timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _DeadlineExpired
        return min(remaining, self.stall_s)

    def send(self, data) -> None:
        self.sock.settimeout(self._timeout())
        super().send(data)

    def response_class(self, sock, **kwargs) -> http.client.HTTPResponse:
        return http.client.HTTPResponse(_TimedReader(sock, self.deadline, self.stall_s), **kwargs)


@dataclass
class ResponseCapture:
    status: int | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: str | None = None
    lines: list[dict] | None = None


@dataclass
class Exchange:
    request: dict
    response: ResponseCapture
    elapsed_s: float = 0.0
    error: str | None = None
    payload: object = None
    chunks: list = field(default_factory=list)
    done: bool = False


def _nonfinite(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def parse_json(data: str | bytes) -> object:
    return json.loads(data, parse_constant=_nonfinite)


def _capture_line(exchange: Exchange, raw: bytes, started: float) -> None:
    line = raw.decode("utf-8", errors="surrogateescape")
    exchange.response.lines.append({"elapsed_s": time.monotonic() - started, "line": line})
    # An unterminated line can be captured after an EOF or a timeout.
    if exchange.done or exchange.error == "http_error":
        return
    if not raw.endswith(b"\n") or not raw.startswith(b"data:"):
        return
    data = raw[5:].removeprefix(b" ").rstrip(b"\r\n")
    if data == b"[DONE]":
        exchange.done = True
    else:
        exchange.chunks.append(parse_json(data))


def request(
    method: str,
    url: str,
    body: object,
    *,
    deadline_s: float,
    stall_s: float,
    sse: bool = False,
) -> Exchange:
    """Make one request and retain the response entity or timestamped SSE lines."""
    if not all(math.isfinite(value) and value > 0 for value in (deadline_s, stall_s)):
        raise ValueError("timeouts must be positive and finite")
    port, target = loopback_url(url)
    started = time.monotonic()
    deadline = started + deadline_s
    encoded = None if body is None else json.dumps(body, ensure_ascii=True).encode("utf-8")
    headers = {
        "Host": f"{urlsplit(url).hostname}:{port}",
        "Accept-Encoding": "identity",
        "Accept": "text/event-stream" if sse else "application/json",
        "Connection": "close",
    }
    if encoded is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded))
    elif method.upper() in {"POST", "PUT", "PATCH"}:
        headers["Content-Length"] = "0"
    exchange = Exchange(
        request={
            "method": method,
            "url": url,
            "headers": headers,
            "body": None if encoded is None else encoded.decode("ascii"),
        },
        response=ResponseCapture(lines=[] if sse else None),
    )
    body_bytes = bytearray()
    pending = bytearray()
    connection = None
    response = None
    try:
        connection = _TimedConnection(port, deadline, stall_s)
        connection.connect()
        connection.request(method, target, body=encoded, headers=headers)

        response = connection.getresponse()
        exchange.response.status = response.status
        exchange.response.headers = response.getheaders()
        if response.status != 200:
            exchange.error = "http_error"

        while not exchange.done:
            if time.monotonic() >= deadline:
                raise _DeadlineExpired
            received = response.read1(8192)
            if not received:
                if response.length:
                    exchange.error = exchange.error or "connection_error"
                break
            if sse:
                pending.extend(received)
                while (line_end := pending.find(b"\n")) >= 0:
                    line = bytes(pending[:line_end + 1])
                    del pending[:line_end + 1]
                    try:
                        _capture_line(exchange, line, started)
                    except (ValueError, RecursionError):
                        exchange.error = "bad_json"
            else:
                body_bytes.extend(received)
        if not sse and exchange.error is None:
            exchange.payload = parse_json(bytes(body_bytes))
    except _DeadlineExpired:
        exchange.error = "timeout_deadline"
    except TimeoutError:
        exchange.error = "timeout_deadline" if time.monotonic() >= deadline else "timeout_stall"
    except (OSError, http.client.HTTPException):
        exchange.error = exchange.error or "connection_error"
    except (ValueError, RecursionError):
        exchange.error = "bad_json"
    finally:
        if response is not None:
            response.close()
        if connection is not None:
            connection.close()
        if sse and pending:
            _capture_line(exchange, bytes(pending), started)
        if not sse:
            exchange.response.body = body_bytes.decode("utf-8", errors="surrogateescape")
        exchange.elapsed_s = time.monotonic() - started
    return exchange
