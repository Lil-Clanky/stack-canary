import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def forbid_real_launchctl(monkeypatch):
    original = subprocess.Popen.__init__

    def guarded(process, args, *positional, **kwargs):
        executable = kwargs.get("executable") or (args[0] if isinstance(args, (list, tuple)) else args)
        if executable and Path(os.fsdecode(executable)).name.lower() in {"launchctl", "launchctl.exe"}:
            pytest.fail("launchctl must be mocked in tests")
        original(process, args, *positional, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)
