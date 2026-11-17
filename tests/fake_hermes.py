"""A Hermes CLI fixture that exercises the recording proxy without a model."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def main():
    args = sys.argv[1:]
    mode = Path(__file__).with_name("mode.txt").read_text().strip()
    if args == ["--sleep-child"]:
        time.sleep(120)
        return 0
    if args == ["chat", "--help"]:
        if mode == "unsupported_cli":
            print("--provider -m")
        else:
            print("--oneshot --query-file -q --provider --base-url -m -t "
                  "-Q --format {text,stream-json} --safe-mode --api-key --reasoning "
                  "--max-turns --run-budget --yolo --source --in")
        return 0
    if mode == "hang":
        time.sleep(120)
    if mode == "cli_usage":
        print("usage: hermes chat [options]", file=sys.stderr)
        return 2
    if mode == "draining":
        print("Error: Gateway draining: not_ready", file=sys.stderr)
        return 1
    base = args[args.index("--base-url") + 1]
    model = args[args.index("-m") + 1]
    query = Path(args[args.index("--query-file") + 1]).read_text()
    token = re.search(r"STACK-CANARY-[a-f0-9]{8}", query)[0]
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": query}]}).encode()
    opener = build_opener(ProxyHandler({}))
    if mode in {"probe_pass", "probe_fail"}:
        try:
            opener.open(base.removesuffix("/v1") + "/api/v1/models", timeout=2).close()
        except HTTPError as error:
            error.close()
    try:
        headers = {"Content-Type": "application/json", "Authorization": "Bearer test-key"}
        with opener.open(Request(base + "/chat/completions", body, headers), timeout=2) as response:
            json.load(response)
    except HTTPError:
        return 1

    if mode not in {"no_file", "probe_fail"}:
        Path("canary.txt").write_bytes(("incorrect" if mode == "wrong_content" else token + "\n").encode())
    if mode != "no_write_event":
        print(json.dumps({"type": "tool_use", "name": "write_file"}))
    print(json.dumps({"type": "result", "exit_code": 1 if mode == "nonzero_exit" else 0}))
    if mode == "leftover_child":
        options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
        child = subprocess.Popen([sys.executable, "-B", __file__, "--sleep-child"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **options)
        print(json.dumps({"type": "fixture_child", "pid": child.pid}))
    return 1 if mode == "nonzero_exit" else 0


if __name__ == "__main__":
    raise SystemExit(main())
