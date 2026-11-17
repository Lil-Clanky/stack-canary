"""Attach Hermes v0.23.2 chat to a foreground gateway owned by this attempt."""

import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

TAG = "v0.23.2"
COMMIT = "ae2a81ccc00e9b7c06301523e5bb09608397149c"
# --force skips service-manager conflict probes. The host lock has its own isolated namespace.
GATEWAY_ARGS = ("gateway", "run", "--force")


def _ready_endpoint(gateway, home, startup_s, discover):
    deadline = time.monotonic() + startup_s
    while gateway.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            record = json.loads((home / "gateway.pid").read_bytes())
        except (FileNotFoundError, ValueError):
            record = None
        if isinstance(record, dict) and record.get("pid") == gateway.pid:
            result = discover(home, timeout=min(remaining, 1))
            endpoint = result.endpoint
            if result.state == "ready" and endpoint is not None:
                if (endpoint.supervisor != "none" or endpoint.control_home is not None
                        or gateway.poll() is not None):
                    raise RuntimeError("Foreground Hermes gateway no longer owns the attempt")
                return endpoint
        time.sleep(min(0.1, remaining))
    raise RuntimeError("Foreground Hermes gateway did not become ready")


def _attach_chat(home, endpoint):
    from hermes_cli import gateway_chat, gateway_runtime
    from hermes_cli.gateway_client import GatewayClientError, connect_gateway
    from hermes_constants import get_hermes_home

    def refuse_autostart(*args, **kwargs):
        raise GatewayClientError("Foreground Hermes gateway required")

    gateway_runtime.ensure_gateway_runtime = refuse_autostart
    if get_hermes_home().resolve() != home:
        raise RuntimeError("Hermes resolved an unexpected profile")
    # The tagged client's explicit endpoint path mints its ticket without
    # calling ensure_gateway_runtime, even if the gateway exits before the dial.
    gateway_chat.connect_gateway = lambda: connect_gateway(endpoint=endpoint)
    from hermes_cli.main import main

    return main()


def run(argv, startup_s, log_path):
    from hermes_cli.gateway_runtime import discover_gateway_endpoint

    home = Path(os.environ["HERMES_HOME"]).resolve()
    # The tagged gateway falls back to /tmp if its Unix socket path is too long.
    if len(os.fsencode(home / "gateway.sock")) > 100:
        raise RuntimeError("Canary home is too long for an isolated Hermes control socket")
    with Path(log_path).open("wb") as log:
        gateway = subprocess.Popen([argv[0], *GATEWAY_ARGS], stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT)
        try:
            endpoint = _ready_endpoint(gateway, home, startup_s, discover_gateway_endpoint)
            return _attach_chat(home, endpoint)
        finally:
            with suppress(ProcessLookupError):
                gateway.terminate()
            try:
                gateway.wait(timeout=3)
            except subprocess.TimeoutExpired:
                gateway.kill()
                gateway.wait()


if __name__ == "__main__":
    startup_s, log_path, *argv = sys.argv[1:]
    sys.argv = argv
    try:
        raise SystemExit(run(sys.argv, float(startup_s), log_path))
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
