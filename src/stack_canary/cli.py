"""Command-line interface for Stack Canary."""

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import __version__, config, hermes_verify, maintenance, managed, notifications, publish, report, results, schedule, watch
from .checks import CHECKS
from .checks.api import run_checks
from .runtime_ollama import OllamaRuntime, canary_home
from .transport import loopback_url, request


def _csv(value: str) -> tuple[str, ...]:
    terms = [item.strip() for item in value.split(",")]
    if not all(terms):
        raise argparse.ArgumentTypeError("expected a comma-separated list of nonempty names")
    return tuple(dict.fromkeys(terms))


def _get(endpoint: str, path: str, settings: config.Settings, secret_headers: dict | None = None) -> dict:
    exchange = request(
        "GET", endpoint + path, None,
        deadline_s=settings.deadline_s, stall_s=settings.stall_s, secret_headers=secret_headers,
    )
    if exchange.error:
        raise ValueError(f"{path}: {exchange.error}")
    if not isinstance(exchange.payload, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return exchange.payload


def _openai_root(url: str) -> str:
    """Return the server root of a loopback OpenAI-compatible base URL that ends in /v1."""
    _, target = loopback_url(url)
    if target.rstrip("/") != "/v1":
        raise ValueError("the OpenAI endpoint must be a base URL ending in /v1, with no query")
    return url.rstrip("/").removesuffix("/v1")


def _summary_rows(models: list[dict]) -> list[dict]:
    status = {"pass": "pass", "error": "error"}
    return [{"model": model["name"], "check": check["check_id"], "status": status.get(check["outcome"], "fail"),
             "passed": sum(attempt["outcome"] == "pass" for attempt in check["attempts"]),
             "attempts": len(check["attempts"])}
            for model in models for check in model["checks"]]


def _write_atomic(path: Path, text: str) -> None:
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".summary-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
        openai = args.openai_endpoint is not None
        overrides = {
            key: getattr(args, key) for key in ("repeats", "results_dir", "context_length")
            if getattr(args, key) is not None
        }
        if openai and args.repeats is None:
            overrides["repeats"] = 1
        settings = replace(settings, **overrides)
        if settings.repeats < 1:
            raise ValueError("repeats must be a positive integer")
        if settings.context_length < 1:
            raise ValueError("context_length must be a positive integer")
        if not settings.results_dir:
            raise ValueError("results_dir must be a nonempty path")
        if args.api_key_env and not openai:
            raise ValueError("--api-key-env requires --openai-endpoint")
        if args.summary_json and args.ollama is not None:
            raise ValueError("--summary-json requires --endpoint or --openai-endpoint")
        models = tuple(dict.fromkeys([*(args.models or ()), *(args.model or ())]))
        if not models and not openai:
            raise ValueError("--models is required")
        if args.hermes and args.ollama is None:
            raise ValueError("--hermes requires --ollama")
        defaults = (*CHECKS, "B6") if args.hermes else tuple(CHECKS)
        checks = args.checks if args.checks is not None else defaults
        unknown = [check for check in checks if check not in {*CHECKS, "B6"}]
        if unknown:
            raise ValueError(f"unknown checks: {', '.join(unknown)}")
        if "B6" in checks and not args.hermes:
            raise ValueError("B6 requires --hermes")
        if "B6" in checks and settings.context_length < 64_000:
            raise ValueError("B6 needs context_length of at least 64000, the Hermes minimum")

        if args.ollama is not None:
            return managed.run(args.ollama, models, checks, settings,
                               hermes=args.hermes, dry_run=args.dry_run)
        if args.dry_run:
            raise ValueError("--dry-run requires --ollama")
        if openai:
            # No Ollama steps: no /api/version, /api/tags or thinking detection.
            kind, endpoint, version, digests, headers = "openai", args.openai_endpoint.rstrip("/"), None, {}, None
            root = _openai_root(args.openai_endpoint)
            if args.api_key_env:
                key = os.environ.get(args.api_key_env)
                if not key:
                    raise ValueError(f"environment variable {args.api_key_env} is not set")
                if any(ord(char) < 32 or ord(char) == 127 for char in key):
                    raise ValueError(f"environment variable {args.api_key_env} contains control characters")
                headers = {"Authorization": f"Bearer {key}"}
            if not models:
                listed = _get(root, "/v1/models", settings, headers).get("data")
                models = tuple(dict.fromkeys(item["id"] for item in listed or []
                                             if isinstance(item, dict) and isinstance(item.get("id"), str)))
                if not models:
                    raise ValueError("/v1/models: expected a data array with model ids")
        else:
            kind, headers = "ollama", None
            _, target = loopback_url(args.endpoint)
            if target != "/" or "?" in args.endpoint or "#" in args.endpoint:
                raise ValueError("the endpoint must be a base URL with no path, query, or fragment")
            endpoint = root = args.endpoint.rstrip("/")
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

    models_run = []
    all_pass = True
    for name in models:
        thinking = name in settings.thinking_models and not openai
        records = run_checks(root, name, checks, settings, thinking=thinking, secret_headers=headers)
        all_pass = all_pass and all(record["outcome"] == "pass" for record in records)
        models_run.append({"name": name, "digest": digests.get(name), "thinking": thinking, "checks": records})

    result = {
        "schema_version": 1,
        "canary_version": __version__,
        "host": results.host_metadata(),
        "endpoint": endpoint,
        "endpoint_kind": kind,
        "runtime_version": version,
        "settings": {
            "repeats": settings.repeats,
            "results_dir": settings.results_dir,
            "deadline_s": settings.deadline_s,
            "stall_s": settings.stall_s,
            "thinking_models": settings.thinking_models,
            "checks": checks,
        },
        "models": models_run,
    }
    try:
        path = results.write(Path(settings.results_dir), result)
    except OSError as error:
        print(f"canary: could not write results: {error}", file=sys.stderr)
        return 2
    if args.summary_json:
        summary = {"endpoint": endpoint, "endpoint_kind": kind, "results": _summary_rows(models_run)}
        try:
            _write_atomic(Path(args.summary_json), json.dumps(summary, indent=2) + "\n")
        except OSError as error:
            print(f"canary: could not write summary {args.summary_json}: {error}", file=sys.stderr)
            return 2

    _print_table(models_run, checks)
    print(f"Results written to {path}")
    return 0 if all_pass else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="canary")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run compatibility checks")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--endpoint", help="base URL of a loopback server")
    source.add_argument("--openai-endpoint", metavar="URL",
                        help="loopback base URL of an OpenAI-compatible server, ending in /v1")
    source.add_argument("--ollama", help="last:N or a comma-separated list of Ollama versions")
    run.add_argument("--hermes", help="latest, last:N or a Hermes release tag")
    run.add_argument("--models", type=_csv, help="comma-separated model names")
    run.add_argument("--model", action="append", help="one model name, repeatable; with --openai-endpoint the "
                     "models default to GET /v1/models")
    run.add_argument("--api-key-env", metavar="NAME",
                     help="environment variable holding the Bearer key for --openai-endpoint")
    run.add_argument("--summary-json", metavar="PATH", help="also write a short JSON summary of each check")
    run.add_argument("--checks", type=_csv, help="comma-separated check IDs (default: A1–A6)")
    run.add_argument("--repeats", "--attempts", type=int, dest="repeats",
                     help="attempts per model and check (default: 3, or 1 with --openai-endpoint)")
    run.add_argument("--results-dir", help="output directory (default: results)")
    run.add_argument("--context-length", type=int,
                     help="managed Ollama context; Layer A needs far less than the B6 default of 65536")
    run.add_argument("--dry-run", action="store_true", help="show the managed plan without writing state")
    reporting = commands.add_parser("report", help="generate a compatibility report and repro drafts")
    reporting.add_argument("--results", action="extend", nargs="+", type=Path, metavar="DIR")
    reporting.add_argument("--out", type=Path, default=Path("report/index.html"))
    reporting.add_argument("--repro-dir", type=Path, default=Path("repro"))
    reporting.add_argument("--all-models", action="store_true", help="include models from older runs")
    for command, help_text in (
        ("watch", "run checks for newly released Ollama versions"),
        ("gc", "remove old managed runtimes and Hermes installs"),
    ):
        action = commands.add_parser(command, help=help_text)
        action.add_argument("--dry-run", action="store_true", help="show the plan without applying it")
    scheduling = commands.add_parser("schedule", help="manage the optional macOS watcher")
    actions = scheduling.add_subparsers(dest="action", required=True)
    install = actions.add_parser("install", help="install the daily LaunchAgent")
    install.add_argument("--at", required=True, metavar="HH:MM")
    install.add_argument("--replace", action="store_true")
    install.add_argument("--print", dest="print_only", action="store_true")
    actions.add_parser("uninstall", help="remove the Stack Canary LaunchAgent")
    actions.add_parser("status", help="show the schedule and last watch result")
    hermes = commands.add_parser("hermes", help="manage verified Hermes tags")
    verification = hermes.add_subparsers(dest="action", required=True).add_parser(
        "verify", help="verify foreground B6 and isolation for a tag")
    verification.add_argument("tag")
    notify = commands.add_parser("notify", help="test configured notification channels")
    notify.add_argument("--test", action="store_true", required=True)
    commands.add_parser("publish", help="publish sanitized artifacts to the matrix branch")
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    try:
        if args.command == "watch":
            return watch.run(config.load(), dry_run=args.dry_run)
        if args.command == "gc":
            runtime = OllamaRuntime(canary_home(), config.load())
            if args.dry_run:
                maintenance.collect(runtime, dry_run=True)
            else:
                with runtime.exclusive():
                    maintenance.collect(runtime)
            return 0
        if args.command == "schedule":
            return schedule.run(args)
        if args.command == "publish":
            runtime = OllamaRuntime(canary_home(), config.load())
            with runtime.exclusive():
                publish.publish(runtime)
            return 0
        if args.command == "notify":
            sent = notifications.send(config.load(), {"status": "test notification"})
            for channel, status in sent.items():
                print(f"{channel}: {status}")
            return 0
        if args.command == "hermes":
            runtime = OllamaRuntime(canary_home(), config.load(), fetch=managed.fetch_url)
            with runtime.exclusive():
                row = hermes_verify.verify(runtime, args.tag)
                if row["status"] == "verified":
                    return 0
                summary = {"status": "Hermes verification failed", "regressions": [], "infra_errors": [],
                           "isolation_errors": [args.tag] if row["evidence"]["isolation_incident"] else [],
                           "new_unverified_hermes": [], "failed_hermes": [args.tag]}
                path = runtime.root / "hermes-verification-summary.md"
                if runtime.settings.notify_command:
                    report._write(path, runtime.root, "# " + notifications.body(summary) + "\n")
                watch._notify(runtime.settings, summary, path, 2)
                return 2
        output, drafts = report.generate(args.results, args.out, args.repro_dir, all_models=args.all_models)
    except managed.INFRA_ERRORS as error:
        print(f"canary: {error}", file=sys.stderr)
        return 2
    print(f"Report written to {output} ({drafts} repro drafts)")
    return 0
