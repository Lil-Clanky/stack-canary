"""Run Layer A against verified, isolated Ollama releases."""

import http.client
import platform
import subprocess
import sys
import tarfile
from dataclasses import asdict
from pathlib import Path

from . import __version__, results
from .checks.api import run_checks
from .harness_hermes import HermesHarness, hermes_tags
from .releases import fetch_url, ollama_releases, version_key
from .runtime_ollama import OllamaError, OllamaRuntime, canary_home, derived_name, model_key, safe_path

INFRA_ERRORS = (OllamaError, OSError, ValueError, tarfile.TarError,
                subprocess.SubprocessError, http.client.HTTPException)


def _error(error, stage):
    return {
        "code": error.code if isinstance(error, OllamaError) else "runtime_error",
        "message": str(error),
        "stage": stage,
    }


def _diagnostics(server):
    return {
        "path": str(server.log_path),
        "tail": server.log_tail,
        "matches": server.log_matches,
    }


def _dry_run(runtime, releases, names):
    print(f"Canary home: {runtime.root}")
    print(f"Free disk space: {runtime.free_bytes() / 1024 ** 3:.2f} GiB")
    for release in releases:
        print(f"Ollama {release.version}: download {release.asset_name} if not installed")
        print(f"  {release.asset_url}")
        print(f"  SHA-256 {release.sha256}")
    if runtime.settings.seed_store:
        print(f"Seed master by cloning missing files from: {runtime.settings.seed_store}")
    print(f"Pull into master if missing: {', '.join(names)}")


def _too_new(requires, version):
    if not requires:
        return False
    if not isinstance(requires, str):
        raise ValueError("expected a version string in model requires")
    return version_key(requires.removeprefix("v")) > version_key(version)


def _check_version(runtime, release, checks, models, report, harnesses):
    runtime.clone_store(release.version)
    server = runtime.server(release)
    report["server_env"] = server.environment
    try:
        with server:
            tags = runtime.tags(server.endpoint)
            for model in models:
                if _too_new(model["requires"], release.version):
                    model["outcome"] = "n/a"
                    model["checks"] = [
                        {"check_id": check, "outcome": "n/a", "attempts": []}
                        for check in checks
                    ]
                    continue

                name, derived = model["name"], model["derived_model"]
                model["digest"] = tags.get(model_key(name))
                model["derived_digest"] = tags.get(model_key(derived))
                if not model["digest"] or not model["derived_digest"]:
                    raise OllamaError("model_digest_missing", f"missing model digest for {name}")
                thinking = "thinking" in model["capabilities"] or name in runtime.settings.thinking_models
                model["thinking"] = thinking
                gpu = runtime.warm_up(server.endpoint, derived, thinking=thinking)
                model["gpu"] = gpu
                if gpu["offload"] == 0:
                    raise OllamaError("cpu_fallback", f"{name}: /api/ps reports zero GPU offload")
                for check_id in checks:
                    if check_id == "B6":
                        model["checks"].extend(
                            {**harness.check(server.endpoint, derived), "harness_tag": harness.tag}
                            for harness in harnesses
                        )
                    else:
                        model["checks"].extend(run_checks(
                            server.endpoint, derived, [check_id], runtime.settings, thinking=thinking,
                        ))
    finally:
        report["log"] = _diagnostics(server)


def _run(runtime, releases, names, checks, output, tags):
    failures = {}
    installed = []
    for release in releases:
        try:
            runtime.install(release)
            installed.append(release)
        except INFRA_ERRORS as error:
            failures[release.version] = _error(error, "install")

    master = []
    master_version = None
    master_logs = {}
    for release in installed:
        server = None
        try:
            server = runtime.server(release, master=True)
            master = runtime.prepare_master(names, release, server=server)
            master_version = release.version
            break
        except INFRA_ERRORS as error:
            failures[release.version] = _error(error, "prepare_master")
        finally:
            if server is not None:
                master_logs[release.version] = _diagnostics(server)

    identifier = results.run_id()
    harnesses = []
    harness_errors = []
    if master:
        for tag in tags:
            try:
                harnesses.append(HermesHarness(runtime.root, tag, runtime.settings, runtime.guard))
            except INFRA_ERRORS as error:
                harness_errors.append({"tag": tag, **_error(error, "prepare_harness")})
    exit_code = 0
    for release in releases:
        version = release.version
        models = [
            {**model, "checks": [], "gpu": None} for model in master
        ] if master else [
            {"name": name, "derived_model": derived_name(name), "digest": None,
             "derived_digest": None, "capabilities": [], "requires": None,
             "context_length": None, "checks": [], "gpu": None}
            for name in names
        ]
        report = {
            "schema_version": 2,
            "canary_version": __version__,
            "host": {"platform": platform.platform(), "python_version": platform.python_version()},
            "runtime": {
                "version": version, "tag": release.tag, "asset": release.asset_name,
                "sha256": release.sha256, "path": str(runtime.root / "runtimes/ollama" / version),
            },
            "settings": {**asdict(runtime.settings), "checks": checks},
            "master_version": master_version,
            "server_env": {},
            "models": models,
        }
        if version in master_logs:
            report["master_log"] = master_logs[version]
        if tags:
            metadata = [harness.metadata for harness in harnesses]
            report["harness"] = metadata[0] if len(metadata) == len(tags) == 1 else {"releases": metadata}
            if harness_errors:
                report["harness_errors"] = harness_errors
        try:
            if version not in failures:
                _check_version(runtime, release, checks, models, report, harnesses)
        except INFRA_ERRORS as error:
            failures[version] = _error(error, "check_version")
        if harness_errors and version not in failures:
            failures[version] = harness_errors[0]

        if version in failures:
            report["infra_error"] = failures[version]
            print(f"Ollama {version}: {failures[version]['message']}", file=sys.stderr)
            exit_code = 2
        elif any(check["outcome"] not in {"pass", "n/a"} for model in models for check in model["checks"]):
            exit_code = max(exit_code, 1)

        try:
            path = results.write(output, report, root=runtime.root, identifier=identifier, version=version)
            print(f"Ollama {version}: results written to {path}")
        except (OSError, ValueError) as error:
            print(f"Ollama {version}: could not write results: {error}", file=sys.stderr)
            exit_code = 2
    return exit_code


def run(spec, names, checks, settings, *, hermes=None, dry_run=False):
    try:
        aliases = {model_key(name) for name in names}
        derived = {model_key(derived_name(name)) for name in names}
        if len(aliases) != len(names) or len(derived) != len(names) or aliases & derived:
            raise ValueError("model names or derived canary names collide")

        runtime = OllamaRuntime(canary_home(), settings, fetch=fetch_url)
        output = safe_path(runtime.root, Path(settings.results_dir))
        releases = ollama_releases(spec, asset_name=settings.asset_name, fetch=fetch_url)
        tags = hermes_tags(hermes, fetch=fetch_url) if "B6" in checks else []
        if dry_run:
            _dry_run(runtime, releases, names)
            if tags:
                print(f"Hermes releases for B6: {', '.join(tags)}")
            return 0
        with runtime.exclusive():
            return _run(runtime, releases, names, checks, output, tags)
    except INFRA_ERRORS as error:
        print(f"canary: {error}", file=sys.stderr)
        return 2
