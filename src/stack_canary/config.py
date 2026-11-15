"""Load optional phase 2 run settings from canary.toml."""

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    repeats: int = 3
    results_dir: str = "results"
    deadline_s: float = 2.0
    stall_s: float = 1.5
    thinking_models: tuple[str, ...] = ()


def load(path: Path = Path("canary.toml")) -> Settings:
    try:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
    except FileNotFoundError:
        return Settings()

    unknown = values.keys() - {item.name for item in fields(Settings)}
    if unknown:
        raise ValueError(f"unknown config settings: {', '.join(sorted(unknown))}")
    if "thinking_models" in values:
        models = values["thinking_models"]
        if not isinstance(models, list) or not all(isinstance(model, str) and model for model in models):
            raise ValueError("thinking_models must be an array of nonempty model names")
        values["thinking_models"] = tuple(models)

    settings = Settings(**values)
    if type(settings.repeats) is not int or settings.repeats < 1:
        raise ValueError("repeats must be a positive integer")
    if not isinstance(settings.results_dir, str) or not settings.results_dir:
        raise ValueError("results_dir must be a nonempty path")
    for key in ("deadline_s", "stall_s"):
        value = getattr(settings, key)
        if type(value) not in (int, float) or not 0 < value < float("inf"):
            raise ValueError(f"{key} must be a positive finite number")
    return settings
