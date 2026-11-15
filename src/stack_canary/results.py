"""Write a timestamped endpoint run as JSON."""

import json
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile


def write(directory: Path, result: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"{timestamp}_endpoint.json"
    stream = NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, prefix=".endpoint-", delete=False
    )
    temporary = Path(stream.name)
    try:
        with stream:
            json.dump(result, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
