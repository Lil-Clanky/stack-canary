"""Send fixed, privacy-safe summaries through configured notification channels."""

import re
import subprocess
import sys
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

TITLE = "Stack Canary"
TIMEOUT_S = 15
COUNTS = ("regressions", "infra_errors", "isolation_errors", "unverified_hermes", "failed_hermes")
STATES = {"pass", "error", "checks failed", "nothing new", "test notification", "Hermes verification failed"}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def validate(settings):
    if len(set(settings.notify)) != len(settings.notify) or set(settings.notify) - {"ntfy", "macos"}:
        raise ValueError("notify must contain distinct macos or ntfy channels")
    server = urlsplit(settings.ntfy_server) if isinstance(settings.ntfy_server, str) else None
    if (server is None or server.scheme != "https" or not server.hostname or server.username is not None
            or server.password is not None or server.query or server.fragment):
        raise ValueError("ntfy_server must be an HTTPS URL without credentials, query or fragment")
    topic = settings.ntfy_topic
    if (topic is not None and (not isinstance(topic, str) or not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", topic))
            or ("ntfy" in settings.notify and topic is None)):
        raise ValueError("configured ntfy requires a valid ntfy_topic")


def body(summary):
    state = summary.get("status")
    headline = state if state in STATES else "error"
    counts = ", ".join(f"{label}: {len(summary.get(key) or [])}" for key, label in zip(
        COUNTS, ("regressions", "infrastructure errors", "isolation errors", "unverified Hermes", "failed Hermes")))
    return f"{TITLE} watch — {headline}\n{counts}"


def _macos(message):
    script = 'on run argv\ndisplay notification (item 1 of argv) with title "Stack Canary"\nend run'
    subprocess.run(["/usr/bin/osascript", "-e", script, "--", message],
                   capture_output=True, stdin=subprocess.DEVNULL, check=True, timeout=TIMEOUT_S)


def _ntfy(settings, message, high):
    url = settings.ntfy_server.rstrip("/") + "/" + quote(settings.ntfy_topic, safe="")
    request = Request(url, data=message.encode(), method="POST",
                      headers={"Title": TITLE, "Priority": "high" if high else "default"})
    with build_opener(_NoRedirect()).open(request, timeout=TIMEOUT_S) as response:
        return response.status


def send(settings, summary):
    message = body(summary)
    results = {}
    for channel in settings.notify:
        try:
            if channel == "macos":
                _macos(message)
                results[channel] = "submitted"
            else:
                results[channel] = f"HTTP {_ntfy(settings, message, bool(summary.get('regressions')))}"
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # Exception strings can contain a topic or command arguments.
            print(f"canary: {channel} notification failed ({type(error).__name__})", file=sys.stderr)
            results[channel] = "failed"
    return results
