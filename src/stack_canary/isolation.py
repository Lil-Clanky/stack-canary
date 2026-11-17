"""Take read-only fingerprints of the user's Ollama and Hermes state."""

import hashlib
import http.client
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


_RELEVANT_LAUNCHD_LABEL = re.compile(r"hermes|ollama|nousresearch|canary", re.IGNORECASE)


def _json_key(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash_file(path, root):
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    with resolved.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _walk_error(error):
    raise error


def _entries(directory, root):
    if not directory.is_dir() or not directory.resolve(strict=True).is_relative_to(root):
        return None
    return sorted(
        Path(base) / name
        for base, directories, files in os.walk(directory, onerror=_walk_error)
        for name in (*directories, *files)
    )


def _manifests(directory, root):
    try:
        paths = _entries(directory, root)
        if paths is None:
            return None
        entries = {}
        for path in paths:
            name = path.relative_to(directory).as_posix()
            if path.is_symlink():
                entries[name] = [os.readlink(path), _hash_file(path, root)]
            elif path.is_file():
                entries[name] = _hash_file(path, root)
            else:
                entries[name] = None
        return hashlib.sha256(_json_key(entries).encode()).hexdigest()
    except (OSError, RuntimeError):
        return None


def _blobs(directory, root):
    try:
        paths = _entries(directory, root)
        if paths is None:
            return None
        entries = {}
        for path in paths:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                entries[path.relative_to(directory).as_posix()] = {
                    "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                }
        return entries
    except (OSError, RuntimeError):
        return None


def _file_fingerprint(path, root):
    try:
        return _hash_file(path, root)
    except (OSError, RuntimeError):
        return None


_YAML_KEY = re.compile(r"""^( *)(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)'|([\w.-]+(?: +[\w.-]+)*)):(?:[ \t]+|$)(.*)$""")


def _config_fingerprint(path, root):
    """Fingerprint block mapping keys and their values without retaining raw content."""
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None, {}
        data = resolved.read_bytes()
    except (OSError, RuntimeError):
        return None, {}
    entries = {}
    parents = []
    scalar = None
    for line in data.decode("utf-8-sig", errors="replace").splitlines():
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if scalar is not None and indent > scalar[0]:
            entries[scalar[1]].update(("\n" + line).encode())
            continue
        if not stripped or stripped.startswith("#"):
            continue
        scalar = None
        if stripped == "-" or stripped.startswith("- "):
            # PyYAML can emit sequences aligned with their key. Keep their
            # content under that key and never interpret an item as a key.
            while parents and parents[-1][0] > indent:
                parents.pop()
            if parents:
                name = parents[-1][1]
                entries[name].update(("\n" + line).encode())
                scalar = indent, name
            continue
        while parents and parents[-1][0] >= indent:
            parents.pop()
        match = _YAML_KEY.match(line)
        if not match:
            continue
        _, double, single, plain, value = match.groups()
        key = double if double is not None else single.replace("''", "'") if single is not None else plain
        name = (*parents[-1][1], key) if parents else (key,)
        value = value.strip()
        is_container = not value or value.startswith("#")
        entries[name] = hashlib.sha256(b"" if is_container else value.encode())
        if is_container:
            parents.append((indent, name))
        else:
            scalar = indent, name
    return hashlib.sha256(data).hexdigest(), {key: value.hexdigest() for key, value in entries.items()}


def _launch_agents(home):
    directory = home / "Library/LaunchAgents"
    try:
        root = home.resolve(strict=True)
        if not directory.resolve().is_relative_to(root):
            return {}
        return {path.name: _file_fingerprint(path, root) for path in directory.glob("*.plist")}
    except (OSError, RuntimeError):
        return None


def _api(path):
    # This narrowly scoped probe is read-only. Managed clients still refuse 11434.
    conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=1)
    try:
        conn.request("GET", path)
        with conn.getresponse() as response:
            if response.status == 200:
                payload = json.loads(response.read(2 * 1024 ** 2))
                if path == "/api/tags" and isinstance(payload, dict) and isinstance(payload.get("models"), list):
                    payload["models"].sort(key=_json_key)
                return payload
    except (OSError, ValueError, http.client.HTTPException, RecursionError):
        return None
    finally:
        conn.close()
    return None


def _command(argv, *, accepted=(0,)):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode in accepted else None


def _services():
    if sys.platform != "darwin":
        return None, None
    jobs = _command(["/bin/launchctl", "list"])
    listeners = _command(
        ["/usr/sbin/lsof", "-nP", "-iTCP:9119", "-iTCP:9120", "-sTCP:LISTEN", "-Fpcn"],
        accepted=(0, 1),
    )
    rows = None
    if jobs is not None:
        rows = {}
        for line in jobs.splitlines():
            fields = line.split()
            if len(fields) == 3 and (fields[0] == "-" or fields[0].isdecimal()):
                pid, _, label = fields
                rows[label] = int(pid) if pid != "-" else None
    # Preserve lsof's process-to-listener association while normalizing row order.
    groups = {}
    if listeners is not None:
        pid = None
        for line in listeners.splitlines():
            if line.startswith("p"):
                pid = line[1:]
                groups[pid] = []
            elif pid is not None:
                groups[pid].append(line)
        groups = {pid: sorted(values) for pid, values in groups.items()}
    return rows, groups if listeners is not None else None


def snapshot(home=None):
    home = Path.home() if home is None else Path(home)
    try:
        models = (home / ".ollama/models").resolve()
    except (OSError, RuntimeError):
        models = None
    try:
        hermes = (home / ".hermes").resolve()
    except (OSError, RuntimeError):
        hermes = None
    config_sha, config_keys = _config_fingerprint(hermes / "config.yaml", hermes) if hermes else (None, {})
    jobs, listeners = _services()
    return {
        "manifests_sha256": _manifests(models / "manifests", models) if models else None,
        "manifests_v2_sha256": _manifests(models / "manifests-v2", models) if models else None,
        "blobs": _blobs(models / "blobs", models) if models else None,
        "ollama_version": _api("/api/version"),
        "ollama_tags": _api("/api/tags"),
        "hermes_config_sha256": config_sha,
        "hermes_config_keys": config_keys,
        "hermes_env_sha256": _file_fingerprint(hermes / ".env", hermes) if hermes else None,
        "launchd": jobs,
        "launch_agents": _launch_agents(home),
        "listeners": listeners,
    }


def _relevant_launchd_jobs(jobs):
    if jobs is None:
        return None
    return {label: pid for label, pid in jobs.items() if _RELEVANT_LAUNCHD_LABEL.search(label)}


def compare(before, after):
    diff, warnings = {}, {}
    for key in before.keys() | after.keys():
        previous, current = before.get(key), after.get(key)
        if key == "launchd":
            previous, current = _relevant_launchd_jobs(previous), _relevant_launchd_jobs(current)
        if previous != current:
            target = warnings if key in {"launchd", "listeners"} else diff
            if key == "hermes_config_keys":
                previous, current = previous or {}, current or {}
                diff[key] = {
                    "added": sorted(".".join(name) for name in current.keys() - previous.keys()),
                    "removed": sorted(".".join(name) for name in previous.keys() - current.keys()),
                    "changed": sorted(".".join(name) for name in previous.keys() & current.keys()
                                      if previous[name] != current[name]),
                }
            elif key == "launchd" and isinstance(previous, dict) and isinstance(current, dict):
                added, removed = sorted(current.keys() - previous.keys()), sorted(previous.keys() - current.keys())
                if added or removed:
                    diff[key] = {"added": added, "removed": removed}
                pids = {label: {"before": previous[label], "after": current[label]}
                        for label in sorted(previous.keys() & current.keys()) if previous[label] != current[label]}
                if pids:
                    warnings[key] = pids
            elif key in {"blobs", "launch_agents"} and isinstance(previous, dict) and isinstance(current, dict):
                target[key] = {name: {"before": previous.get(name), "after": current.get(name)}
                               for name in sorted(previous.keys() | current.keys())
                               if previous.get(name) != current.get(name)}
            else:
                target[key] = {"before": previous, "after": current}
    status = "isolation_violated" if diff else "warning" if warnings else "pass"
    return {"status": status, "diff": diff, "warnings": warnings}
