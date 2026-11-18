"""Load optional run settings from canary.toml."""

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .releases import DEFAULT_ASSET, asset_filename, version_key


@dataclass(frozen=True)
class Settings:
    repeats: int = 3
    results_dir: str = "results"
    deadline_s: float = 120
    stall_s: float = 30
    thinking_models: tuple[str, ...] = ()
    asset_name: str = DEFAULT_ASSET
    port: int = 11535
    context_length: int = 65536
    kv_cache_type: str = "q8_0"
    min_free_gb: float = 5
    max_swap_gb: float = 4
    allow_full_copy: bool = False
    seed_store: str | None = None
    hermes_deadline_s: float = 300
    models: tuple[str, ...] = ("llama3.2:3b", "granite4:3b-h", "qwen3.5:0.8b")
    watch_versions: int = 2
    hermes_tag: str = "v0.23.2"
    notify_command: tuple[str, ...] | None = None
    notify_on_success: bool = False
    notify: tuple[str, ...] = ()
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str | None = None
    publish_branch: str | None = None
    publish_gh_user: str | None = None
    keep_runtimes: int = 3
    keep_hermes: int = 2


def load(path: Path = Path("canary.toml")) -> Settings:
    try:
        with path.open("rb") as stream:
            values = tomllib.load(stream)
    except FileNotFoundError:
        return Settings()

    unknown = values.keys() - {item.name for item in fields(Settings)}
    if unknown:
        raise ValueError(f"unknown config settings: {', '.join(sorted(unknown))}")
    for key in ("thinking_models", "models", "notify_command", "notify"):
        if key in values:
            items = values[key]
            if (not isinstance(items, list) or not all(isinstance(item, str) and item.strip() for item in items)
                    or (key in {"models", "notify_command"} and not items)):
                raise ValueError(f"{key} must be an array of nonempty strings")
            values[key] = tuple(items)

    settings = Settings(**values)
    if type(settings.repeats) is not int or settings.repeats < 1:
        raise ValueError("repeats must be a positive integer")
    if not isinstance(settings.results_dir, str) or not settings.results_dir:
        raise ValueError("results_dir must be a nonempty path")
    for key in ("deadline_s", "stall_s", "hermes_deadline_s"):
        value = getattr(settings, key)
        if type(value) not in (int, float) or not 0 < value < float("inf"):
            raise ValueError(f"{key} must be a positive finite number")
    asset_filename(settings.asset_name)
    if type(settings.port) is not int or not 1 <= settings.port <= 65535 or settings.port in {9119, 9120, 11434}:
        raise ValueError("port must be from 1 to 65535, excluding 9119, 9120 and 11434")
    if type(settings.context_length) is not int or settings.context_length < 1:
        raise ValueError("context_length must be a positive integer")
    if not isinstance(settings.kv_cache_type, str) or not settings.kv_cache_type.strip():
        raise ValueError("kv_cache_type must be a nonempty string")
    for key in ("min_free_gb", "max_swap_gb"):
        value = getattr(settings, key)
        if type(value) not in (int, float) or not 0 <= value < float("inf"):
            raise ValueError(f"{key} must be finite and nonnegative")
    if type(settings.allow_full_copy) is not bool:
        raise ValueError("allow_full_copy must be a boolean")
    if settings.seed_store is not None and (
        not isinstance(settings.seed_store, str) or not settings.seed_store.strip()
    ):
        raise ValueError("seed_store must be a nonempty path")
    if type(settings.watch_versions) is not int or not 1 <= settings.watch_versions <= 50:
        raise ValueError("watch_versions must be an integer from 1 to 50")
    if not isinstance(settings.hermes_tag, str) or not settings.hermes_tag.startswith("v"):
        raise ValueError("hermes_tag must be a stable tag such as v0.23.2")
    version_key(settings.hermes_tag[1:])
    for key in ("keep_runtimes", "keep_hermes"):
        value = getattr(settings, key)
        if type(value) is not int or value < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    if type(settings.notify_on_success) is not bool:
        raise ValueError("notify_on_success must be a boolean")
    from .notifications import validate
    validate(settings)
    if settings.publish_branch not in (None, "matrix"):
        raise ValueError("publish_branch must be matrix")
    if settings.publish_gh_user is not None and (
        not isinstance(settings.publish_gh_user, str) or not settings.publish_gh_user.isascii()
        or not settings.publish_gh_user.replace("-", "").isalnum()
    ):
        raise ValueError("publish_gh_user must be a GitHub username")
    if settings.publish_branch and not settings.publish_gh_user:
        raise ValueError("publish_branch requires publish_gh_user")
    return settings
