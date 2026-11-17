"""Commit-bound attestations for the macOS foreground harness."""

import json
from pathlib import Path

from .runtime_ollama import _state_path

SEED_TAG = "v0.23.2"
SEED_COMMIT = "ae2a81ccc00e9b7c06301523e5bb09608397149c"
REGISTRY = Path("hermes-verified.json")


def load(root):
    registry = {SEED_TAG: {"commit": SEED_COMMIT, "verified_at": "2026-11-17T19:39:09Z",
                           "method": "foreground_live_matrix", "status": "verified",
                           "evidence": {"summary": "18 B6 attempts passed with a clean isolation audit"}}}
    path = _state_path(root, REGISTRY)
    if path.exists():
        stored = json.loads(path.read_bytes())
        if not isinstance(stored, dict) or any(not isinstance(row, dict) for row in stored.values()):
            raise ValueError("invalid Hermes verification registry")
        registry.update(stored)
    return registry


def verified_commit(root, tag):
    row = load(root).get(tag, {})
    return row.get("commit") if row.get("status") == "verified" else None


def save(root, registry):
    from .report import _write

    _write(_state_path(root, REGISTRY), root, json.dumps(registry, indent=2, sort_keys=True) + "\n")
