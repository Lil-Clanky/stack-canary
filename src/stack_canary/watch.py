"""Run the configured matrix once for each newly discovered Ollama release."""

import json
import subprocess
import sys
import time
from pathlib import Path

from . import hermes_registry, hermes_verify, isolation, maintenance, managed, notifications, publish, report, results
from .harness_hermes import hermes_tags
from .releases import ollama_releases, version_key
from .runtime_ollama import OllamaError, OllamaRuntime, _state_path, _swap_used_bytes, canary_home, safe_path

HOOK_TIMEOUT_S = 15


def _checks(model, tags, tag):
    for cell in model.get("checks") or []:
        if not isinstance(cell, dict):
            continue
        check = cell.get("check_id")
        if check == "B6":
            default = tag if tags == {tag} else None
            if (cell.get("harness_tag") or default) != tag:
                continue
            cell = report._normalize_b6(cell)
        if check in report.CHECK_IDS:
            yield check, cell


def _tags(data):
    return set(data.get("hermes_tags") or ()) | report._harness_records(data).keys()


def _eligible_history(history):
    return [run for run in history if run.data.get("runtime") and not run.data.get("infra_error")
            and not run.data.get("harness_errors")
            and (run.data.get("isolation") or {}).get("status") != "isolation_violated"]


def _missing_checks(history, settings):
    """Map each version with eligible results to the checks each model still lacks."""
    eligible = _eligible_history(history)
    cells, _, _ = report.merge(eligible)
    accepted = {"pass", "fail", "flaky", "timeout", "n/a"}

    def done(version, model, check):
        cell = cells.get((version, model, check, settings.hermes_tag if check == "B6" else ""))
        return cell is not None and cell.outcome in accepted and not cell.model.get("model_error")

    return {version: {model: [check for check in report.CHECK_IDS if not done(version, model, check)]
                      for model in settings.models}
            for version in {run.version for run in eligible}}


def _regressions(history, current, tag):
    if not current:
        return []
    runs = [*history, *current]
    cells, _, _ = report.merge(runs)
    versions = sorted({run.version for run in runs if run.data.get("runtime")},
                      key=report._version_order, reverse=True)
    regressions = []
    for run in current:
        idx = versions.index(run.version)
        if idx + 1 == len(versions):
            continue
        previous_version = versions[idx + 1]
        for model in run.data["models"]:
            for check, cell in _checks(model, _tags(run.data), tag):
                outcome = cell["outcome"]
                prior = cells.get((previous_version, model["name"], check, tag if check == "B6" else ""))
                if (prior is not None and prior.outcome == "pass" and outcome in report.DRAFT_OUTCOMES
                        and (check != "B6" or cell.get("attribution") == "runtime")):
                    regressions.append({"version": run.version, "previous_version": previous_version,
                                        "model": model["name"], "check": check, "outcome": outcome})
    return regressions


def _record_reports(summary, produced, history, tag):
    current = []
    for data in produced:
        version = data["runtime"]["version"]
        current.append(report.Run(version, None, data))
        summary["ran"].append(version)
        if error := data.get("infra_error"):
            summary["infra_errors"].append({"version": version, **error})
        audit = data.get("isolation") or {}
        if audit.get("status") == "isolation_violated":
            summary["isolation_errors"].append({"version": version, **audit})
        for model in data["models"]:
            if error := model.get("model_error"):
                summary["infra_errors"].append({"version": version, "model": model["name"], **error})
            for check, cell in _checks(model, _tags(data), tag):
                if cell["outcome"] == "error" and not model.get("model_error"):
                    summary["infra_errors"].append({
                        "version": version, "model": model["name"], "check": check,
                        "message": cell.get("reason") or "check error",
                    })
    summary["regressions"] = _regressions(history, current, tag)


def _markdown(summary):
    lines = [
        f"# Stack Canary watch — {summary['status']}", "",
        f"- Selected: {', '.join(summary['selected']) or 'none'}",
        f"- Ran: {', '.join(summary['ran']) or 'none'}",
        f"- Unverified Hermes: {', '.join(summary['unverified_hermes']) or 'none'}",
        f"- Failed Hermes verification: {', '.join(summary.get('failed_hermes', [])) or 'none'}",
        f"- Lowest free disk (bytes): {summary['lowest_free_bytes']}",
        f"- Peak swap (bytes): {summary['peak_swap_used_bytes']}",
        f"- Duration (seconds): {summary['duration_s']:.2f}", "",
        "## Regressions", "",
    ]
    lines.extend(f"- {cell['version']} vs {cell['previous_version']}, {cell['model']} "
                 f"{cell['check']}: {cell['outcome']}" for cell in summary["regressions"])
    if not summary["regressions"]:
        lines.append("- None")
    for key, title in (("infra_errors", "Infrastructure errors"), ("isolation_errors", "Isolation errors")):
        lines.extend(["", f"## {title}", ""])
        lines.extend(f"- {entry.get('version', 'Watch')}: "
                     f"{entry.get('message') or ', '.join(sorted(entry.get('diff', {})))}"
                     for entry in summary[key])
        if not summary[key]:
            lines.append("- None")
    return "\n".join(lines) + "\n"


def _write_summary(root, summary):
    directory = Path.cwd().resolve() / "report"
    for name, body in (("summary.md", _markdown(summary)),
                       ("summary.json", json.dumps(summary, indent=2) + "\n")):
        path, scope = report._scope(directory / name, root, directory)
        report._write(path, scope, body)
    return directory / "summary.md"


def _notify(settings, summary, path, code):
    if (summary["regressions"] or summary["infra_errors"] or summary["isolation_errors"]
            or summary["new_unverified_hermes"] or summary.get("failed_hermes")
            or (code == 0 and settings.notify_on_success)):
        notifications.send(settings, summary)
        if not settings.notify_command:
            return
        try:
            subprocess.run([*settings.notify_command, str(path)], check=True, timeout=HOOK_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"canary: notification hook failed ({type(error).__name__})", file=sys.stderr)


def _watch(runtime, dry_run):
    started = time.monotonic()
    root, settings = runtime.root, runtime.settings
    directory = safe_path(root, Path(settings.results_dir))
    directories = [directory, root / "results", Path.cwd() / "results"]
    seen_path = _state_path(root, Path("watch-hermes.json"))
    summary = {"timestamp": results.run_id(), "models": settings.models, "hermes_tag": settings.hermes_tag,
               "selected": [], "ran": [], "regressions": [], "infra_errors": [], "isolation_errors": [],
               "unverified_hermes": [], "new_unverified_hermes": [], "failed_hermes": []}
    produced = []
    history = []
    code = 0
    changed = False
    try:
        managed.validate_models(settings.models)
        before = None if dry_run else isolation.snapshot()
        history = report.load_runs(directories)
        missing = _missing_checks(history, settings)
        options = {"fetch": managed.fetch_url, "cache_root": root, "read_only": dry_run}
        recent = ollama_releases(f"last:{settings.watch_versions}", asset_name=settings.asset_name, **options)
        everything = {model: report.CHECK_IDS for model in settings.models}
        gaps = {release.version: missing.get(release.version, everything) for release in recent}
        releases = [release for release in recent if any(gaps[release.version].values())]
        # Rerun only what is missing, so a new check or model does not repeat the heavy B6 runs.
        models = tuple(model for model in settings.models if any(gap[model] for gap in gaps.values()))
        checks = tuple(check for check in report.CHECK_IDS
                       if any(check in lacking for gap in gaps.values() for lacking in gap.values()))
        summary["selected"] = [release.version for release in releases]
        tags = hermes_tags("last:50", **options)
        registry = hermes_registry.load(root)
        newer = [tag for tag in tags if version_key(tag[1:]) > version_key(settings.hermes_tag[1:])]
        if newer and hermes_verify.available() and newer[0] not in registry and not dry_run:
            tag = newer[0]
            row = hermes_verify.verify(runtime, tag)
            registry[tag] = row
            if row["status"] != "verified":
                summary["failed_hermes"].append(tag)
                if row["evidence"].get("isolation_incident"):
                    summary["isolation_errors"].append({"tag": tag, "diff": {"hermes_probe": True}})
                code = 2
        summary["unverified_hermes"] = [tag for tag in newer
                                        if registry.get(tag, {}).get("status") != "verified"]
        try:
            saved = json.loads(seen_path.read_bytes())
        except FileNotFoundError:
            saved = []
        if not isinstance(saved, list) or not all(isinstance(tag, str) for tag in saved):
            raise ValueError("invalid watch Hermes history")
        seen = set(saved)
        summary["new_unverified_hermes"] = [tag for tag in summary["unverified_hermes"] if tag not in seen]
        pending = []
        if hermes_verify.available():
            known, _, _ = report.merge(_eligible_history(history))
            for tag in newer:
                if registry.get(tag, {}).get("status") != "verified":
                    continue
                missing = [release for release in recent if any(
                    (release.version, model, "B6", tag) not in known
                    or known[(release.version, model, "B6", tag)].outcome == "error"
                    for model in settings.models)]
                if missing:
                    pending.append((tag, missing))
                    print(f"Watch Hermes {tag}: " + ", ".join(release.version for release in missing))
        changed = bool(releases or pending or summary["new_unverified_hermes"])
        print("Watch Ollama: " + (", ".join(summary["selected"]) or "nothing new"))
        if summary["new_unverified_hermes"]:
            print("New unverified Hermes: " + ", ".join(summary["new_unverified_hermes"]))
        if dry_run:
            return 0
        if releases:
            try:
                code = max(code, managed._run_with_isolation(
                    runtime, models, checks, directory, before,
                    lambda: (releases, [settings.hermes_tag] if "B6" in checks else []), produced,
                ))
            finally:
                _record_reports(summary, produced, history, settings.hermes_tag)
            if code == 0 and any(cell["outcome"] != "pass" for data in produced for model in data["models"]
                                 for _, cell in _checks(model, _tags(data), settings.hermes_tag)):
                code = 1
            if code == 2 and not (summary["infra_errors"] or summary["isolation_errors"]):
                summary["infra_errors"].append({"message": "Managed run failed to persist results"})
        for tag, missing in pending:
            additional = []
            try:
                code = max(code, managed._run_with_isolation(
                    runtime, settings.models, ("B6",), directory, isolation.snapshot(),
                    lambda: (missing, [tag]), additional,
                ))
            finally:
                _record_reports(summary, additional, history, tag)
                produced.extend(additional)
    except managed.INFRA_ERRORS as error:
        if dry_run:
            raise
        summary["infra_errors"].append({"message": str(error)})
        code = 2
    if changed and (history or produced):
        try:
            report.generate(directories, repro_dir=root / "repro")
        except (OSError, ValueError) as error:
            summary["infra_errors"].append({"message": f"Report: {error}"})
            code = 2
    free = [value for row in runtime.disk_trace if (value := row["free_bytes"]) is not None]
    swap = [value for row in runtime.disk_trace if (value := row["swap_used_bytes"]) is not None]
    summary.update({"lowest_free_bytes": min(free) if free else runtime.free_bytes(),
                    "peak_swap_used_bytes": max(swap) if swap else _swap_used_bytes(),
                    "duration_s": time.monotonic() - started, "exit_code": code,
                    "status": {2: "error", 1: "checks failed", 0: "pass" if changed else "nothing new"}[code]})
    path = _write_summary(root, summary)
    if changed:
        try:
            maintenance.collect(runtime)
        except (OSError, ValueError) as error:
            summary["infra_errors"].append({"message": f"GC: {error}"})
            code = 2
            summary.update({"exit_code": code, "status": "error", "duration_s": time.monotonic() - started})
            _write_summary(root, summary)
    if summary["new_unverified_hermes"]:
        report._write(seen_path, root, json.dumps(sorted(seen | set(summary["new_unverified_hermes"]))) + "\n")
    if settings.publish_branch:
        try:
            publish.publish(runtime)
        except managed.INFRA_ERRORS as error:
            summary["infra_errors"].append({"message": f"Publish: {error}"})
            code = 2
            summary.update(exit_code=2, status="error")
            path = _write_summary(root, summary)
    _notify(settings, summary, path, code)
    return code


def run(settings, *, dry_run=False):
    runtime = OllamaRuntime(canary_home(), settings, fetch=managed.fetch_url)
    try:
        with runtime.exclusive():
            return _watch(runtime, dry_run)
    except OllamaError as error:
        if error.code != "home_in_use":
            raise
        print("canary: already running")
        return 0
