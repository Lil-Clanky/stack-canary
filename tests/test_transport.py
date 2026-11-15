import http.client

import pytest

import fake_ollama
from fake_ollama import running
from stack_canary.transport import request


def test_ndjson_keeps_raw_lines_and_delivers_records(monkeypatch):
    raw = b'{"status":"pulling"}\r\n{"status":"success"}'

    def progress(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    monkeypatch.setattr(fake_ollama.Handler, "do_GET", progress)
    seen = []
    with running() as server:
        result = request(
            "GET", server.endpoint, None, deadline_s=2, stall_s=0.3,
            ndjson=True, on_line=seen.append,
        )
    assert result.error is None
    assert result.chunks == seen == [{"status": "pulling"}, {"status": "success"}]
    assert "".join(item["line"] for item in result.response.lines).encode() == raw


@pytest.mark.parametrize("sse", [False, True])
def test_bad_json_keeps_raw_response(monkeypatch, sse):
    raw = b"data: {!\n\ndata: [DONE]\n\n" if sse else b"{!"

    def invalid_json(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    monkeypatch.setattr(fake_ollama.Handler, "do_GET", invalid_json)
    with running() as server:
        result = request("GET", server.endpoint, None, deadline_s=2, stall_s=0.3, sse=sse)
    assert result.error == "bad_json"
    if sse:
        assert "".join(item["line"] for item in result.response.lines).encode() == raw
    else:
        assert result.response.body.encode() == raw


def test_deadline_applies_inside_headers(monkeypatch):
    def drip_header(handler):
        handler.wfile.write(b"HTTP/1.1 200 OK\r\nX-Test: ")
        try:
            while not handler.server.stopping.wait(0.1):
                handler.wfile.write(b"x")
        except OSError:
            pass

    monkeypatch.setattr(fake_ollama.Handler, "do_GET", drip_header)
    with running() as server:
        result = request("GET", server.endpoint, None, deadline_s=1, stall_s=0.5)
    assert result.error == "timeout_deadline"
    assert 0.9 <= result.elapsed_s < 1.9


def test_connection_error(monkeypatch):
    def drop_connection(handler):
        handler.close_connection = True

    monkeypatch.setattr(fake_ollama.Handler, "do_GET", drop_connection)
    with running() as server:
        result = request("GET", server.endpoint, None, deadline_s=1, stall_s=0.3)
    assert result.error == "connection_error"


@pytest.mark.parametrize("url", [
    "http://localhost:11434", "http://127.0.0.1:011434", "http://example.com:8000",
    "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:99999",
    "http://user@127.0.0.1:8000", "https://localhost:8000",
])
def test_disallowed_endpoint_before_connect(monkeypatch, url):
    monkeypatch.setattr(http.client.HTTPConnection, "connect", lambda _: pytest.fail("connected"))
    with pytest.raises(ValueError):
        request("GET", url, None, deadline_s=1, stall_s=0.3)
