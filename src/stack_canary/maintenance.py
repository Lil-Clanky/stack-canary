"""Prune old managed installations while preserving results and shared stores."""

import shutil
from pathlib import Path

from .releases import VERSION, version_key
from .runtime_ollama import _state_path


def _versions(root, parent, prefix=""):
    directory = _state_path(root, Path(parent))
    if not directory.exists():
        return {}
    versions = {}
    for child in directory.iterdir():
        version = child.name.removeprefix(prefix)
        if (child.name.startswith(prefix) and VERSION.fullmatch(version)
                and (child.is_dir() or child.is_symlink())):
            versions[version] = _state_path(root, child.relative_to(root))
    return versions


def collect(runtime, *, dry_run=False):
    """Print and optionally remove expired versioned directories."""
    root, settings = runtime.root, runtime.settings
    ollama = _versions(root, "runtimes/ollama")
    clones = _versions(root, "stores", "ollama-")
    hermes = _versions(root, "harnesses/hermes", "v")
    retain_ollama = set(sorted(ollama, key=version_key, reverse=True)[:settings.keep_runtimes])
    retain_hermes = set(sorted(hermes, key=version_key, reverse=True)[:settings.keep_hermes])
    retain_hermes.add(settings.hermes_tag[1:])
    plan = [
        path for entries, retain in ((ollama, retain_ollama), (clones, retain_ollama), (hermes, retain_hermes))
        for version, path in sorted(entries.items(), key=lambda item: version_key(item[0]))
        if version not in retain
    ]
    free_initial = runtime.free_bytes()
    for path in plan:
        if dry_run:
            print(f"Would delete {path}")
            continue
        before = runtime.free_bytes()
        shutil.rmtree(_state_path(root, path.relative_to(root)))
        print(f"Deleted {path} ({runtime.free_bytes() - before} bytes freed)")
    freed = 0 if dry_run else runtime.free_bytes() - free_initial
    print(f"GC: {freed} bytes freed (measured free disk change)" + (" (dry run)" if dry_run else ""))
