"""Verify a macOS foreground Hermes harness and record its isolation evidence."""

import ast
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import hermes_registry, isolation, report
from .harness_hermes import HermesHarness, _install, hermes_tags
from .releases import ollama_releases, version_key
from .runtime_ollama import OllamaError, _state_path

MODEL = "llama3.2:3b"


def available():
    return sys.platform == "darwin"


def _functions(path):
    return {node.name: node for node in ast.parse(path.read_bytes()).body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def static_check(source):
    client = _functions(source / "hermes_cli/gateway_client.py").get("connect_gateway")
    chat = _functions(source / "hermes_cli/gateway_chat.py").get("run_gateway_chat")
    runtime = _functions(source / "hermes_cli/gateway_runtime.py")
    homes = _functions(source / "hermes_constants.py")
    candidates = homes.get("_iter_real_home_candidates")
    endpoint = bool(client and "endpoint" in {a.arg for a in (*client.args.args, *client.args.kwonlyargs)}
                    and any(isinstance(n, ast.If) and ast.unparse(n.test) == "endpoint is not None"
                            and "_session_ticket" in ast.unparse(n.body)
                            and "ensure_gateway_runtime" not in ast.unparse(n.body)
                            for n in ast.walk(client)))
    chat_attach = bool(chat and "connect_gateway(" in ast.unparse(chat))
    real_home = bool(candidates and "get_real_home" in homes and any(
        isinstance(n, ast.List) and n.elts and "HERMES_REAL_HOME" in ast.unparse(n.elts[0])
        for n in ast.walk(candidates)))
    launchctl = set()
    for folder in ("hermes_cli", "gateway"):
        for path in (source / folder).rglob("*.py"):
            raw = path.read_text(encoding="utf-8")
            if re.search(r'["\x27]launchctl["\x27]', raw):
                launchctl.add("PATH")
            if re.search(r"/(?:usr/)?bin/launchctl\b", raw):
                launchctl.add("absolute")
    discovery = {"discover_gateway_endpoint", "ensure_gateway_runtime"} <= runtime.keys()
    return {"passed": all((endpoint, chat_attach, real_home, discovery)), "explicit_endpoint": endpoint,
            "chat_attach": chat_attach, "real_home": real_home, "discovery": discovery,
            "launchctl": sorted(launchctl),
            "symbols": ["connect_gateway(endpoint=...)", "run_gateway_chat", "discover_gateway_endpoint",
                        "ensure_gateway_runtime", "get_real_home", "_iter_real_home_candidates"]}


def _launchctl(*args):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=10, check=False)


def _owned_job(root, description):
    for line in description.splitlines():
        match = re.fullmatch(r"\s*(?:program\s*=|HERMES_HOME\s*=>)\s*(/[^\r\n]+)", line)
        if match and Path(match[1].strip()).resolve().is_relative_to(root.resolve()):
            return True
    return False


def _cleanup_jobs(root, labels):
    cleaned = []
    for label in labels:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
            continue
        target = f"gui/{os.getuid()}/{label}"
        result = _launchctl("print", target)
        if not result.returncode and _owned_job(root, result.stdout):
            if not _launchctl("bootout", target).returncode:
                cleaned.append(label)
    return cleaned


def _probe(runtime, tag, installed, log):
    history = report.load_runs([runtime.root / runtime.settings.results_dir, runtime.root / "results",
                                Path.cwd() / "results"])
    versions = {run.version for run in history if run.data.get("runtime") and not run.data.get("infra_error")
                and (run.data.get("isolation") or {}).get("status") != "isolation_violated"}
    if not versions:
        raise ValueError("no successfully tested Ollama version for Hermes verification")
    version = max(versions, key=version_key)
    [release] = ollama_releases(version, fetch=runtime.fetch, asset_name=runtime.settings.asset_name)
    runtime.install(release)
    [model] = runtime.prepare_master([MODEL], release)
    if "tools" not in model["capabilities"]:
        raise ValueError("verification model lacks tools")
    runtime.clone_store(version)
    candidate = HermesHarness(runtime.root, tag, runtime.settings, runtime.guard,
                              installer=lambda *_: installed, probe=log)
    with runtime.server(release) as server:
        try:
            runtime.warm_up(server.endpoint, model["derived_model"], thinking=False)
            attempt = candidate.attempt(server.endpoint, model["derived_model"], 1)
        finally:
            # A watchdog kill surfaces as a connection error; report the real cause instead.
            if server.memory_stop:
                raise OllamaError("memory_pressure", server.memory_stop)
    return {"ollama": version, "outcome": attempt["outcome"], "reason": attempt["reason"],
            "attribution": attempt.get("attribution"), "proxy_requests": len(attempt["proxy_exchanges"]),
            "duration_s": attempt["duration_s"]}


def verify(runtime, tag):
    if not available():
        raise ValueError("Hermes verification is available only on macOS")
    from .managed import INFRA_ERRORS

    runtime.guard("verify Hermes")
    before = isolation.snapshot()
    if any(before.get(key) is None for key in ("launchd", "launch_agents")):
        raise ValueError("cannot establish launchd and LaunchAgents baseline")
    registry = hermes_registry.load(runtime.root)
    if hermes_tags(tag, fetch=runtime.fetch) != [tag]:
        raise ValueError("Hermes verification requires one explicit release tag")
    directory = _state_path(runtime.root, Path("hermes-probes"))
    directory.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="verify-", dir=directory))
    log = directory / "launchctl.log"
    shim = directory / "launchctl"
    shim.write_text('#!/bin/sh\nprintf "%s\\n" launchctl "$@" >> "$STACK_CANARY_LAUNCHCTL_LOG"\nexit 1\n')
    shim.chmod(0o700)
    evidence = {}
    commit = None
    try:
        installed = _install(runtime.root, tag, runtime.guard)
        executable, metadata = installed
        commit = metadata.get("commit")
        evidence["static"] = static_check(executable.parent.parent.parent / "src")
        if evidence["static"]["passed"]:
            evidence["probe"] = _probe(runtime, tag, installed, log)
        else:
            evidence["reason"] = "static_contract_missing"
    except (*INFRA_ERRORS, SyntaxError) as error:
        if getattr(error, "code", None) == "memory_pressure":
            raise  # Inconclusive, so keep the previous registry row.
        evidence["reason"] = getattr(error, "code", type(error).__name__)
        print(f"canary: Hermes {tag}: {error}", file=sys.stderr)
    finally:
        after = isolation.snapshot()
        audit = isolation.compare(before, after)
        valid_snapshot = all(after.get(key) is not None for key in ("launchd", "launch_agents"))
        labels = sorted((isolation._relevant_launchd_jobs(after.get("launchd")) or {}).keys()
                        - (isolation._relevant_launchd_jobs(before["launchd"]) or {}).keys())
        plists = sorted((after.get("launch_agents") or {}).keys() - before["launch_agents"].keys())
        evidence.update(new_labels=labels, new_plists=plists, isolation=audit["status"],
                        isolation_incident=audit["status"] == "isolation_violated",
                        isolation_changes=sorted(audit["diff"]),
                        shim_empty=not log.exists() or log.stat().st_size == 0,
                        snapshots_complete=valid_snapshot)
        if labels:
            evidence["booted_out"] = _cleanup_jobs(runtime.root, labels)
    probe = evidence.get("probe", {})
    good = (re.fullmatch(r"[0-9a-f]{40}", commit or "") and probe.get("outcome") == "pass"
            and evidence["shim_empty"] and valid_snapshot and not labels and not plists
            and not evidence["isolation_incident"])
    if not good:
        evidence.setdefault("reason", "isolation_incident" if evidence["isolation_incident"] else
                            "launchctl_attempted" if not evidence["shim_empty"] else
                            probe.get("reason") or "probe_incomplete")
    row = {"commit": commit, "verified_at": datetime.now(UTC).isoformat(), "method": "foreground_probe",
           "status": "verified" if good else "unsupported", "evidence": evidence}
    registry[tag] = row
    hermes_registry.save(runtime.root, registry)
    print(f"Hermes {tag}: {row['status']}" + (f" ({evidence['reason']})" if not good else ""))
    return row
