import http.client
import json
from threading import Event

import pytest

from fake_ollama import Handler, running
from stack_canary.proxy import RecordingProxy, _Handler, broken_completion


def _client(proxy):
    return http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=2)


def test_proxy_relays_stream_before_completion(monkeypatch):
    proceed = Event()

    def staged(handler, body, mode):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        handler._chunk(b'data: {"choices":[]}\n\n')
        if proceed.wait(2):
            handler._chunk(b"data: [DONE]\n\n")
        handler.wfile.write(b"0\r\n\r\n")

    monkeypatch.setattr(Handler, "_stream", staged)
    with running() as upstream, RecordingProxy(upstream.endpoint, timeout=2) as proxy:
        client = _client(proxy)
        try:
            client.request("POST", "/v1/chat/completions", json.dumps({"model": "a", "stream": True}))
            response = client.getresponse()
            first = response.readline()
            assert first == b'data: {"choices":[]}\n'
            assert not proceed.is_set()
            proceed.set()
            assert response.read() == b"\ndata: [DONE]\n\n"
        finally:
            proceed.set()
            client.close()
    exchange, = proxy.exchanges
    assert exchange["error"] is None
    lines = exchange["response"]["lines"]
    assert lines[0]["line"] == first.decode()
    assert [line["elapsed_s"] for line in lines] == sorted(line["elapsed_s"] for line in lines)


def test_proxy_rejects_alternate_upstream():
    for address in ("http://example.invalid:3000", "http://127.0.0.1:11434", "http://127.0.0.1:9119"):
        with pytest.raises(ValueError):
            RecordingProxy(address)
    with running() as upstream, RecordingProxy(upstream.endpoint) as proxy:
        client = _client(proxy)
        try:
            client.request("GET", "http://127.0.0.1:11434/api/version")
            assert client.getresponse().status == 400
        finally:
            client.close()
    assert proxy.exchanges == []


def test_proxy_records_missing_sse_done():
    with running("no_done") as upstream, RecordingProxy(upstream.endpoint) as proxy:
        client = _client(proxy)
        try:
            client.request("POST", "/v1/chat/completions", json.dumps({"model": "a", "stream": True}))
            assert b"data:" in client.getresponse().read()
        finally:
            client.close()
    assert proxy.exchanges[0]["error"] == "no_done"
    assert broken_completion(proxy.exchanges[0])


@pytest.mark.parametrize(("content_type", "body", "code"), [
    ("application/json", b"{malformed", "bad_json"),
    ("text/event-stream", b"data: {malformed\n\ndata: [DONE]\n\n", "bad_sse"),
])
def test_proxy_records_malformed_completion(monkeypatch, content_type, body, code):
    def reply(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    monkeypatch.setattr(Handler, "do_POST", reply)
    with running() as upstream, RecordingProxy(upstream.endpoint) as proxy:
        client = _client(proxy)
        try:
            client.request("POST", "/v1/chat/completions?trace=1")
            assert client.getresponse().read() == body
        finally:
            client.close()
    exchange, = proxy.exchanges
    assert exchange["error"] == code and broken_completion(exchange)


def test_proxy_records_upstream_exception_with_detail(monkeypatch):
    def disconnect(handler):
        handler.close_connection = True

    monkeypatch.setattr(Handler, "do_POST", disconnect)
    with running() as upstream, RecordingProxy(upstream.endpoint) as proxy:
        client = _client(proxy)
        try:
            client.request("POST", "/v1/chat/completions")
            assert client.getresponse().status == 502
        finally:
            client.close()
    exchange, = proxy.exchanges
    assert exchange["error"] == "upstream_error"
    assert exchange["upstream_error"]["type"] == "RemoteDisconnected"
    assert exchange["upstream_error"]["message"]
    assert exchange["response"]["status"] is None
    assert not broken_completion(exchange)


def test_proxy_distinguishes_client_disconnect_from_no_done(monkeypatch):
    def disconnect(*_):
        raise BrokenPipeError("simulated client disconnect")

    monkeypatch.setattr(_Handler, "end_headers", disconnect)
    with running("no_done") as upstream, RecordingProxy(upstream.endpoint) as proxy:
        client = _client(proxy)
        try:
            client.request("POST", "/v1/chat/completions", b'{"model":"a","stream":true}')
            with pytest.raises(http.client.RemoteDisconnected):
                client.getresponse()
        finally:
            client.close()
    exchange, = proxy.exchanges
    assert exchange["error"] == "client_disconnect"
    assert exchange["client_disconnect"] == {"type": "BrokenPipeError", "message": "simulated client disconnect"}
    assert "upstream_error" not in exchange
    assert not broken_completion(exchange)
