import os
from pathlib import Path

import pytest

from stack_canary.cli import main
from test_runtime_ollama import home


def test_gc_dry_run_and_keep_rules(home, capsys):
    Path("canary.toml").write_text("keep_runtimes = 2\nkeep_hermes = 1\n", encoding="utf-8")
    retained = ["results", "stores/master", "uv-cache", "runtimes/ollama/1.10.0",
                "runtimes/ollama/2.0.0", "stores/ollama-1.10.0", "stores/ollama-2.0.0",
                "harnesses/hermes/v0.23.2", "harnesses/hermes/v0.25.0"]
    obsolete = ["runtimes/ollama/1.9.0", "stores/ollama-1.9.0", "harnesses/hermes/v0.24.0"]
    for name in [*retained, *obsolete]:
        path = home / name
        path.mkdir(parents=True)
        (path / "fixture").write_bytes(b"x")
    assert main(["gc", "--dry-run"]) == 0
    preview = capsys.readouterr().out
    for name in obsolete:
        assert f"Would delete {home / name}" in preview
        assert (home / name).is_dir()
    assert not (home / ".run-lock.guard").exists()
    assert main(["gc"]) == 0
    outcome = capsys.readouterr().out
    assert "bytes freed" in outcome
    assert all(not (home / name).exists() for name in obsolete)
    assert all((home / name / "fixture").read_bytes() == b"x" for name in retained)


def test_gc_refuses_versioned_symlink_outside_home(home):
    external = home.parent / "untouched"
    external.mkdir()
    (external / "proof").write_text("outside", encoding="utf-8")
    target = home / "runtimes/ollama/0.1.0"
    target.parent.mkdir(parents=True)
    try:
        target.symlink_to(external, target_is_directory=True)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("requires Windows symlink privileges")
        raise
    assert main(["gc"]) == 2
    assert target.is_symlink()
    assert (external / "proof").read_text(encoding="utf-8") == "outside"
