from stack_canary import notifications
from stack_canary.config import Settings
from stack_canary.cli import main


def test_ntfy_uses_post_and_only_fixed_text(monkeypatch):
    calls = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            return Response()

    monkeypatch.setattr(notifications, "build_opener", lambda *_: Opener())
    config = Settings(notify=("ntfy",), ntfy_topic="fixture-private-topic")
    result = notifications.send(config, {"status": "pass", "regressions": [{"path": "/Users/person/secret"}]})
    req, timeout = calls[0]
    assert result == {"ntfy": "HTTP 200"}
    assert req.method == "POST" and req.get_header("Priority") == "high" and timeout == 15
    assert b"regressions: 1" in req.data and b"secret" not in req.data
    assert b"fixture-private-topic" not in req.data


def test_macos_notification_passes_message_as_data(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)))
    notifications._macos('quotes " and backslash \\')
    args, kw = calls[0]
    assert args[-2:] == ["--", 'quotes " and backslash \\']
    assert args[2].startswith("on run argv") and kw["timeout"] == 15


def test_notification_failure_does_not_expose_topic_or_change_cli_exit(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "canary.toml").write_text('notify=["ntfy"]\nntfy_topic="fixture-private-topic"\n')

    def fail(*_):
        raise OSError("test error fixture-private-topic")

    monkeypatch.setattr(notifications, "_ntfy", fail)
    assert main(["notify", "--test"]) == 0
    captured = capsys.readouterr()
    assert "notification failed (OSError)" in captured.err
    assert "fixture-private-topic" not in captured.err + captured.out
