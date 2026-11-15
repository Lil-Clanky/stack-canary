"""Write timestamped results as JSON."""

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .releases import version_key
from .runtime_ollama import safe_path


def run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def write(directory: Path, result: dict, *, root=None, identifier=None, version=None) -> Path:
    if version is not None:
        version_key(version)
    suffix = "endpoint" if version is None else f"ollama-{version}"
    filename = f"{identifier or run_id()}_{suffix}.json"
    directory = directory if root is None else safe_path(root, directory)
    path = directory / filename
    if root is not None:
        path = safe_path(root, path)
    directory.mkdir(parents=True, exist_ok=True)
    stream = NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, prefix=".endpoint-", delete=False
    )
    temporary = Path(stream.name)
    try:
        with stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
        if root is not None:
            safe_path(root, temporary).replace(safe_path(root, path))
        else:
            temporary.replace(path)
    finally:
        if root is not None:
            temporary = safe_path(root, temporary)
        temporary.unlink(missing_ok=True)
    return path
