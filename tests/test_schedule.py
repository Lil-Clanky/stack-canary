import os
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from stack_canary import isolation, schedule
from stack_canary.cli import main
from test_runtime_ollama import home


@pytest.fixture
def mac(home, monkeypatch):
    monkeypatch.setattr(schedule, "sys", SimpleNamespace(platform="darwin", stderr=sys.stderr))
    monkeypatch.setattr(schedule, "os", SimpleNamespace(environ=os.environ, getuid=lambda: 501))
    account = home.parent / "account"
    account.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: account))
    calls = []
    loaded = [False]

    def launchctl(argv, **kwargs):
        calls.append(argv)
        assert kwargs["timeout"] == 15
        assert argv[0] == "/bin/launchctl"
        if argv[1] in {"print", "bootout"} and not loaded[0]:
            return SimpleNamespace(returncode=3, stderr="Could not find service", stdout="")
        if argv[1] == "bootstrap":
            loaded[0] = True
        elif argv[1] == "bootout":
            loaded[0] = False
        return SimpleNamespace(returncode=0, stderr="", stdout="\tstate = running\n\tlast exit code = 0\n")

    monkeypatch.setattr(schedule.subprocess, "run", launchctl)
    return account / "Library/LaunchAgents" / schedule.PLIST_NAME, calls


def test_print_plist_is_read_only_and_uses_defaults(mac, home, monkeypatch, capsys):
    monkeypatch.delenv("CANARY_HOME")
    assert main(["schedule", "install", "--at", "02:15", "--print"]) == 0
    plist = plistlib.loads(capsys.readouterr().out.encode())
    log = str(Path.home() / ".stack-canary/logs/watch.log")
    assert plist == {
        "Label": schedule.LABEL,
        "ProgramArguments": [str(Path.cwd() / ".venv/bin/canary"), "watch"],
        "WorkingDirectory": str(Path.cwd()),
        "StartCalendarInterval": {"Hour": 2, "Minute": 15},
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": log, "StandardErrorPath": log,
        "RunAtLoad": False, "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10,
    }
    assert mac[1] == [] and not mac[0].parent.exists() and not home.exists()


def test_install_replace_status_and_uninstall(mac, home, capsys):
    path, calls = mac
    target = "gui/501/com.stackcanary.watch"
    install = ["schedule", "install", "--at", "03:04"]
    assert main(install) == 0
    assert calls == [
        ["/bin/launchctl", "print", target],
        ["/bin/launchctl", "bootstrap", "gui/501", str(path)],
        ["/bin/launchctl", "print", target],
    ]
    data = plistlib.loads(path.read_bytes())
    assert data["EnvironmentVariables"]["CANARY_HOME"] == str(home)
    assert data["StandardOutPath"] == str(home / "logs/watch.log")
    assert main(install) == 2
    assert "already exists" in capsys.readouterr().err
    calls.clear()
    assert main(["schedule", "install", "--at", "00:10", "--replace"]) == 0
    assert [call[1] for call in calls] == ["print", "bootout", "bootstrap", "print"]
    assert plistlib.loads(path.read_bytes())["StartCalendarInterval"] == {"Hour": 0, "Minute": 10}
    Path("report").mkdir()
    Path("report/summary.md").write_text("# Stack Canary watch summary\n", encoding="utf-8")
    assert main(["schedule", "status"]) == 0
    out = capsys.readouterr().out
    assert "Plist: present" in out and "Launchd state: running" in out
    assert "Last exit: 0" in out and "00:10 local time" in out
    assert "Last summary: Stack Canary watch summary" in out
    other = path.with_name("unrelated.plist")
    other.touch()
    assert main(["schedule", "uninstall"]) == 0
    assert main(["schedule", "uninstall"]) == 0
    assert not path.exists() and other.is_file()
    assert calls[-2:] == [["/bin/launchctl", "bootout", target]] * 2


def test_schedule_refuses_non_macos(home, monkeypatch, capsys):
    monkeypatch.setattr(schedule, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    monkeypatch.setattr(schedule, "_launchctl", lambda *a, **k: pytest.fail("unexpected launchctl"))
    assert main(["schedule", "status"]) == 2
    assert "only on macOS" in capsys.readouterr().err
    assert not home.exists()


def test_failed_replace_restores_previous_plist(mac, monkeypatch):
    path, _ = mac
    assert main(["schedule", "install", "--at", "02:15"]) == 0
    original = path.read_bytes()
    launchctl = schedule.subprocess.run
    attempted = False

    def fail_once(argv, **kwargs):
        nonlocal attempted
        if argv[1] == "bootstrap" and not attempted:
            attempted = True
            return SimpleNamespace(returncode=5, stderr="fixture bootstrap failure", stdout="")
        return launchctl(argv, **kwargs)

    monkeypatch.setattr(schedule.subprocess, "run", fail_once)
    assert main(["schedule", "install", "--at", "03:30", "--replace"]) == 2
    assert path.read_bytes() == original
    assert schedule._launchctl("print", "gui/501/com.stackcanary.watch")


def test_audit_records_only_exact_schedule_exemption():
    label, plist = schedule.LABEL, schedule.PLIST_NAME
    before = {"launchd": {label: None}, "launch_agents": {plist: "old"}}
    after = {"launchd": {label: 51}, "launch_agents": {plist: "new"}}
    audit = isolation.compare(before, after)
    assert audit["status"] == "pass"
    assert audit["diff"] == audit["warnings"] == {}
    assert audit["schedule"] == {
        label: {"before": {label: None}, "after": {label: 51}},
        plist: {"before": {plist: "old"}, "after": {plist: "new"}},
    }
    after["launchd"][label + ".another"] = None
    after["launch_agents"][plist + ".another.plist"] = "changed"
    audit = isolation.compare(before, after)
    assert audit["status"] == "isolation_violated"
    assert audit["diff"]["launchd"] == {"added": [label + ".another"], "removed": []}
    assert plist + ".another.plist" in audit["diff"]["launch_agents"]
