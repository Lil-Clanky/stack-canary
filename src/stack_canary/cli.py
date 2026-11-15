"""Command-line interface for Stack Canary."""

import argparse
import platform
import sys
from dataclasses import asdict, replace
from pathlib import Path

from . import __version__, config, results
from .checks import CHECKS
from .checks.api import run_attempt
from .outcome import aggregate
from .transport import loopback_url, request


def _csv(value: str) -> tuple[str, ...]:
    terms = [item.strip() for item in value.split(",")]
    if not all(terms):
        raise argparse.ArgumentTypeError("expected a comma-separated list of nonempty names")
    return tuple(dict.fromkeys(terms))


def _get(endpoint: str, path: str, settings: config.Settings) -> dict:
    exchange = request(
        "GET", endpoint + path, None,
        deadline_s=settings.deadline_s, stall_s=settings.stall_s,
    )
    if exchange.error:
        raise ValueError(f"{path}: {exchange.error}")
    if not isinstance(exchange.payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return exchange.payload


def _tag_digests(tags: list) -> dict[str, str]:
    digests = {}
    for tag in tags:
        if isinstance(tag, dict) and isinstance(tag.get("digest"), str):
            for key in ("name", "model"):
                name = tag.get(key)
                if isinstance(name, str):
                    digests[name] = tag["digest"]
    return digests


def _print_table(models: list[dict], checks: tuple[str, ...]) -> None:
    model_width = max(len("MODEL"), *(len(model["name"]) for model in models))
    cell_width = max(len(outcome) for outcome in ("pass", "fail", "timeout", "flaky"))
    print(f"{'MODEL':<{model_width}}  " + "  ".join(f"{name:<{cell_width}}" for name in checks).rstrip())
    for model in models:
        cells = [f"{check['outcome']:<{cell_width}}" for check in model["checks"]]
        print(f"{model['name']:<{model_width}}  " + "  ".join(cells).rstrip())


def _run(args: argparse.Namespace) -> int:
    try:
        _, target = loopback_url(args.endpoint)
        if target != "/" or "?" in args.endpoint or "#" in args.endpoint:
            raise ValueError("the endpoint must be a base URL with no path, query, or fragment")
        endpoint = args.endpoint.rstrip("/")
        settings = config.load()
        overrides = {
            key: getattr(args, key) for key in ("repeats", "results_dir")
            if getattr(args, key) is not None
        }
        settings = replace(settings, **overrides)
        if settings.repeats < 1:
            raise ValueError("repeats must be a positive integer")
        if not settings.results_dir:
            raise ValueError("results_dir must be a nonempty path")
        checks = args.checks if args.checks is not None else tuple(CHECKS)
        unknown = [check for check in checks if check not in CHECKS]
        if unknown:
            raise ValueError(f"unknown checks: {', '.join(unknown)}")

        version = _get(endpoint, "/api/version", settings).get("version")
        if not isinstance(version, str) or not version:
            raise ValueError("/api/version: expected a version string")
        tags = _get(endpoint, "/api/tags", settings).get("models")
        if not isinstance(tags, list):
            raise ValueError("/api/tags: expected a models array")
        digests = _tag_digests(tags)
    except (ValueError, OSError) as error:
        print(f"canary: {error}", file=sys.stderr)
        return 2

    models = []
    all_pass = True
    for name in args.models:
        thinking = name in settings.thinking_models
        model = {"name": name, "digest": digests.get(name), "thinking": thinking, "checks": []}
        for check_id in checks:
            attempts = [
                run_attempt(
                    check_id, endpoint, name, number, thinking=thinking,
                    deadline_s=settings.deadline_s, stall_s=settings.stall_s,
                )
                for number in range(1, settings.repeats + 1)
            ]
            outcome = aggregate(attempt["outcome"] for attempt in attempts)
            model["checks"].append({"check_id": check_id, "outcome": outcome, "attempts": attempts})
            all_pass = all_pass and outcome == "pass"
        models.append(model)

    result = {
        "schema_version": 1,
        "canary_version": __version__,
        "host": {"platform": platform.platform(), "python_version": platform.python_version()},
        "endpoint": endpoint,
        "runtime_version": version,
        "settings": {**asdict(settings), "checks": checks},
        "models": models,
    }
    try:
        path = results.write(Path(settings.results_dir), result)
    except OSError as error:
        print(f"canary: could not write results: {error}", file=sys.stderr)
        return 2

    _print_table(models, checks)
    print(f"Results written to {path}")
    return 0 if all_pass else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="canary")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run compatibility checks")
    run.add_argument("--endpoint", required=True, help="base URL of the loopback fake server")
    run.add_argument("--models", required=True, type=_csv, help="comma-separated model names")
    run.add_argument("--checks", type=_csv, help="comma-separated check IDs (default: A1–A5)")
    run.add_argument("--repeats", type=int, help="attempts per model and check (default: 3)")
    run.add_argument("--results-dir", help="output directory (default: results)")
    commands.add_parser("report", help="generate a compatibility report")
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    print("not implemented yet")
    return 0
