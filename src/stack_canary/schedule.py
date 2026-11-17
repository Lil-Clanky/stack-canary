"""Manage Stack Canary's opt-in macOS LaunchAgent."""

import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

from .runtime_ollama import _state_path, canary_home

LABEL = "com.stackcanary.watch"
PLIST_NAME = LABEL + ".plist"
JOB_PATH = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _plist(repo, root, hour, minute):
    log = str(_state_path(root, Path("logs/watch.log")))
    env = {"PATH": JOB_PATH}
    if os.environ.get("CANARY_HOME"):
        env["CANARY_HOME"] = str(root)
    return {
        "Label": LABEL,
        "ProgramArguments": [str(repo / ".venv/bin/canary"), "watch"],
        "WorkingDirectory": str(repo),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "EnvironmentVariables": env,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }


def _launchctl(*args, missing_ok=False):
    result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True,
                            errors="replace", stdin=subprocess.DEVNULL, timeout=15)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if missing_ok and any(reason in detail.lower() for reason in (
            "could not find service", "no such process", "service not found",
        )):
            return None
        raise OSError(f"launchctl {args[0]} failed ({result.returncode}): {detail}")
    return result.stdout


def _atomic_plist(path, data):
    with NamedTemporaryFile(dir=path.parent, prefix=f".{PLIST_NAME}.", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            temporary.replace(path)
        finally:
            stream.close()
            temporary.unlink(missing_ok=True)


def _job_summary(output):
    if output is None:
        return "Launchd state: not loaded\nLast exit: unavailable"
    state = re.search(r"(?m)^\s*state\s*=\s*(.+)$", output)
    last_exit = re.search(r"(?m)^\s*last (?:exit code|terminating signal)\s*=\s*(.+)$", output)
    return (f"Launchd state: {state[1] if state else 'loaded'}\n"
            f"Last exit: {last_exit[1] if last_exit else 'unavailable'}")


def _next_time(path):
    now = datetime.now()
    try:
        with path.open("rb") as stream:
            interval = plistlib.load(stream)["StartCalendarInterval"]
        next_time = now.replace(hour=interval["Hour"], minute=interval["Minute"], second=0, microsecond=0)
    except (OSError, ValueError, TypeError, KeyError):
        return "unavailable"
    if next_time <= now:
        next_time += timedelta(days=1)
    return next_time.strftime("%Y-%m-%d %H:%M local time")


def _install(path, data, domain, target, root, *, replace):
    installed = _launchctl("print", target, missing_ok=True) is not None
    if (installed or path.exists()) and not replace:
        raise ValueError(f"{LABEL} already exists. Use --replace to update it")
    original = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    _state_path(root, Path("logs")).mkdir(parents=True, exist_ok=True)
    if installed:
        _launchctl("bootout", target, missing_ok=True)
    try:
        _atomic_plist(path, data)
        _launchctl("bootstrap", domain, str(path))
    except (OSError, subprocess.SubprocessError) as error:
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_plist(path, original)
                if installed:
                    _launchctl("bootstrap", domain, str(path))
        except (OSError, subprocess.SubprocessError) as rollback_error:
            error.add_note(f"Could not restore previous schedule: {rollback_error}")
            print(error.__notes__[-1], file=sys.stderr)
        raise
    print(_job_summary(_launchctl("print", target)))


def run(args):
    if sys.platform != "darwin":
        print("canary: schedule is available only on macOS", file=sys.stderr)
        return 2
    repo = Path.cwd().resolve()
    root = canary_home()
    data = None
    if args.action == "install":
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", args.at):
            raise ValueError("schedule --at requires a local time in HH:MM format")
        hour, minute = (int(part) for part in args.at.split(":"))
        data = plistlib.dumps(_plist(repo, root, hour, minute))
        if args.print_only:
            print(data.decode("utf-8"), end="")
            return 0
    home = Path.home().resolve()
    path = _state_path(home, Path("Library/LaunchAgents") / PLIST_NAME)
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"
    if args.action == "install":
        _install(path, data, domain, target, root, replace=args.replace)
    elif args.action == "uninstall":
        _launchctl("bootout", target, missing_ok=True)
        path.unlink(missing_ok=True)
        print(f"Uninstalled {LABEL}")
    else:
        present = path.is_file()
        output = _launchctl("print", target, missing_ok=True)
        print(f"Plist: {'present' if present else 'absent'}")
        print(_job_summary(output))
        print(f"Next scheduled time: {_next_time(path) if present and output is not None else 'unavailable'}")
        summary = _state_path(repo / "report", Path("summary.md"))
        headline = "unavailable"
        if summary.is_file():
            with summary.open(encoding="utf-8") as stream:
                headline = stream.readline().strip().removeprefix("# ")
        print(f"Last summary: {headline}")
    return 0
