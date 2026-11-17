"""Command-line interface for Stack Canary."""

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from . import __version__, config, managed, report, results
from .checks import CHECKS
from .checks.api import run_checks
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
        if args.hermes and args.ollama is None:
            raise ValueError("--hermes requires --ollama")
        defaults = (*CHECKS, "B6") if args.hermes else tuple(CHECKS)
        checks = args.checks if args.checks is not None else defaults
        unknown = [check for check in checks if check not in {*CHECKS, "B6"}]
        if unknown:
            raise ValueError(f"unknown checks: {', '.join(unknown)}")
        if "B6" in checks and not args.hermes:
            raise ValueError("B6 requires --hermes")

        if args.ollama is not None:
            return managed.run(args.ollama, args.models, checks, settings,
                               hermes=args.hermes, dry_run=args.dry_run)
        if args.dry_run:
            raise ValueError("--dry-run requires --ollama")
        _, target = loopback_url(args.endpoint)
        if target != "/" or "?" in args.endpoint or "#" in args.endpoint:
            raise ValueError("the endpoint must be a base URL with no path, query, or fragment")
        endpoint = args.endpoint.rstrip("/")
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
        records = run_checks(endpoint, name, checks, settings, thinking=thinking)
        all_pass = all_pass and all(record["outcome"] == "pass" for record in records)
        models.append({"name": name, "digest": digests.get(name), "thinking": thinking, "checks": records})

    result = {
        "schema_version": 1,
        "canary_version": __version__,
        "host": results.host_metadata(),
        "endpoint": endpoint,
        "runtime_version": version,
        "settings": {
            "repeats": settings.repeats,
            "results_dir": settings.results_dir,
            "deadline_s": settings.deadline_s,
            "stall_s": settings.stall_s,
            "thinking_models": settings.thinking_models,
            "checks": checks,
        },
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
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--endpoint", help="base URL of a loopback server")
    source.add_argument("--ollama", help="last:N or a comma-separated list of Ollama versions")
    run.add_argument("--hermes", help="latest, last:N or a Hermes release tag")
    run.add_argument("--models", required=True, type=_csv, help="comma-separated model names")
    run.add_argument("--checks", type=_csv, help="comma-separated check IDs (default: A1–A5)")
    run.add_argument("--repeats", type=int, help="attempts per model and check (default: 3)")
    run.add_argument("--results-dir", help="output directory (default: results)")
    run.add_argument("--dry-run", action="store_true", help="show the managed plan without writing state")
    reporting = commands.add_parser("report", help="generate a compatibility report and repro drafts")
    reporting.add_argument("--results", action="extend", nargs="+", type=Path, metavar="DIR")
    reporting.add_argument("--out", type=Path, default=Path("report/index.html"))
    reporting.add_argument("--repro-dir", type=Path, default=Path("repro"))
    reporting.add_argument("--all-models", action="store_true", help="include models from older runs")
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    try:
        output, drafts = report.generate(args.results, args.out, args.repro_dir, all_models=args.all_models)
    except (OSError, ValueError) as error:
        print(f"canary: {error}", file=sys.stderr)
        return 2
    print(f"Report written to {output} ({drafts} repro drafts)")
    return 0
