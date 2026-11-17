"""Install tagged Hermes releases and run the isolated B6 file-write check."""

import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import deque
from contextlib import suppress
from pathlib import Path

from .outcome import aggregate
from .proxy import RecordingProxy
from .releases import _api_json, _stable_version, fetch_url, version_key
from .runtime_ollama import OllamaError, _state_path, safe_path

REPOSITORY = "https://github.com/NousResearch/hermes-agent.git"
API = "https://api.github.com/repos/NousResearch/hermes-agent/releases"
REQUIRED = {"--oneshot", "--provider", "--base-url", "-m", "-t"}
OPTIONAL = {
    "-Q", "--format", "--safe-mode", "--api-key", "--reasoning", "--max-turns",
    "--run-budget", "--yolo", "--source", "--in",
}
API_KEY = "stack-canary-loopback"


def _tag_version(tag):
    if not tag.startswith("v"):
        raise ValueError(f"unsupported Hermes tag: {tag}")
    version = version_key(tag[1:])
    if version[0] >= 2000 or version < (0, 21, 5):
        raise ValueError(f"unsupported Hermes tag: {tag}")
    return version


def hermes_tags(spec, *, fetch=fetch_url):
    if spec == "latest":
        spec = "last:1"
    if not spec.startswith("last:"):
        _tag_version(spec)
        row = _api_json(f"{API}/tags/{spec}", fetch)
        if _stable_version(row) != spec[1:]:
            raise ValueError(f"Hermes {spec} is not a stable release")
        return [spec]
    count = spec.removeprefix("last:")
    if not count.isdecimal() or not 1 <= int(count) <= 50:
        raise ValueError("Hermes last:N requires N from 1 to 50")
    listing = _api_json(API + "?per_page=50", fetch)
    if not isinstance(listing, list):
        raise ValueError("expected a Hermes release list")
    tags = set()
    for row in listing:
        if _stable_version(row) is None:
            continue
        tag = row["tag_name"]
        try:
            _tag_version(tag)
        except ValueError:
            continue
        tags.add(tag)
    if not tags:
        raise ValueError("no stable Hermes releases found")
    return sorted(tags, key=_tag_version, reverse=True)[:int(count)]


def _clean_env(home, tmp):
    # Keep operating system essentials. Provider credentials, proxy settings and
    # inherited agent configuration have no role in an isolated custom provider.
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
               "LANG", "LC_ALL", "TZ"}
    return {
        **{key: value for key, value in os.environ.items() if key.upper() in allowed},
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"),
        "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "NO_PROXY": "127.0.0.1,localhost,ollama.localhost",
    }


def _canary_dir(root, relative):
    path = _state_path(root, relative)
    path.mkdir(parents=True, exist_ok=True)
    return path


def hermes_env(root, tag, workdir):
    _tag_version(tag)
    hermes_home = _canary_dir(root, Path("homes") / f"hermes-{tag}")
    home = _canary_dir(root, hermes_home.relative_to(root) / "home")
    tmp = _canary_dir(root, home.relative_to(root) / "tmp")
    system_path = os.defpath
    if os.name == "nt":
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        system_path = os.pathsep.join((str(windows / "System32"), str(windows)))
    return {
        **_clean_env(home, tmp),
        "PATH": system_path,
        "HERMES_HOME": str(hermes_home),
        "HERMES_WRITE_SAFE_ROOT": str(safe_path(root, workdir)),
        "OPENAI_API_KEY": API_KEY,
    }


def _run(argv, cwd, env, *, timeout):
    result = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise OllamaError("hermes_setup_failed", f"{Path(argv[0]).name}: {result.stderr[-3000:].strip()}")
    return result.stdout.strip()


def _bin(venv, name):
    return venv / ("Scripts" if os.name == "nt" else "bin") / (name + ".exe" if os.name == "nt" else name)


def _dependencies(root, src, venv, env):
    with safe_path(root, src / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]
    extra = "all" if "all" in project.get("optional-dependencies", {}) else None
    requirement = str(src) + (f"[{extra}]" if extra else "")
    lock = safe_path(root, src / "pm/lock.json")
    python_version = "3.13"
    if lock.is_file():
        pin = json.loads(lock.read_text(encoding="utf-8")).get("packages", {}).get("python", {}).get("version")
        if isinstance(pin, str) and re.fullmatch(r"3\.\d+(?:\.\d+)?(?:\+\d+)?", pin):
            python_version = pin.split("+", 1)[0]

    python = _bin(venv, "python")
    if uv := shutil.which("uv"):
        env.update({
            "UV_CACHE_DIR": str(_canary_dir(root, Path("uv-cache"))), "UV_LINK_MODE": "clone",
            "UV_PYTHON_INSTALL_DIR": str(_canary_dir(root, Path("uv-python"))),
            "UV_PROJECT_ENVIRONMENT": str(venv),
        })
        if safe_path(root, src / "uv.lock").is_file():
            args = [uv, "sync", "--frozen", "--no-default-groups", "--python", python_version]
            if extra:
                args.extend(["--extra", extra])
            _run(args, src, env, timeout=1200)
            return "uv_sync_editable"
        _run([uv, "venv", "--python", python_version, str(venv)], src, env, timeout=600)
        _run([uv, "pip", "install", "--python", str(python), "-e", requirement], src, env, timeout=1200)
        return "uv_pip_editable"

    minor = python_version.split(".")[:2]
    interpreter = shutil.which("python" + ".".join(minor))
    if not interpreter and [str(part) for part in sys.version_info[:2]] == minor:
        interpreter = sys.executable
    if not interpreter:
        raise OllamaError("hermes_setup_failed", f"Hermes requires uv or Python {'.'.join(minor)}")
    _run([interpreter, "-m", "venv", str(venv)], src, env, timeout=600)
    _run([str(python), "-m", "pip", "install", "-e", requirement], src, env, timeout=1200)
    return "pip_editable"


def _install(root, tag, guard):
    prefix = _canary_dir(root, Path("harnesses/hermes") / tag)
    install_home = _canary_dir(root, prefix.relative_to(root) / "install-home")
    tmp = _canary_dir(root, install_home.relative_to(root) / "tmp")
    env = {**_clean_env(install_home, tmp), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
           "PIP_CONFIG_FILE": os.devnull, "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    src = _state_path(root, prefix.relative_to(root) / "src")
    venv = _state_path(root, prefix.relative_to(root) / "venv")
    marker = _state_path(root, prefix.relative_to(root) / ".complete")
    partial = _state_path(root, prefix.relative_to(root) / ".complete.part")
    executable = safe_path(root, _bin(venv, "hermes"))
    python = _bin(venv, "python")
    if marker.is_file() and executable.is_file():
        metadata = json.loads(marker.read_text(encoding="utf-8"))
        if metadata.get("install_method"):
            return executable, metadata

    guard("clone Hermes source")
    marker.unlink(missing_ok=True)
    partial.unlink(missing_ok=True)
    for path in (src, venv):
        if path.exists():
            shutil.rmtree(safe_path(root, path))
    _run(["git", "clone", "--depth", "1", "--branch", tag, REPOSITORY, str(src)],
         prefix, env, timeout=600)
    commit = _run(["git", "rev-parse", "HEAD"], src, env, timeout=30)
    guard("install Hermes dependencies")
    method = _dependencies(root, src, venv, env)
    guard("finish installing Hermes")
    if not executable.is_file():
        raise OllamaError("hermes_setup_failed", f"installed package has no Hermes entry point: {executable}")
    metadata = {"tag": tag, "commit": commit, "install_method": method,
                "python_version": _run([str(python), "--version"], prefix, env, timeout=30)}
    try:
        partial.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        safe_path(root, partial).replace(safe_path(root, marker))
    finally:
        safe_path(root, partial).unlink(missing_ok=True)
    return executable, metadata


def detected_flags(help_text):
    flags = set(re.findall(r"(?<![\w-])(?:--[a-z][a-z0-9-]*|-[A-Za-z])(?![\w-])", help_text))
    flags.intersection_update(REQUIRED | OPTIONAL | {"--query-file", "-q"})
    if "stream-json" not in help_text:
        flags.discard("--format")
    return flags


def _local_ollama_name():
    try:
        addresses = socket.getaddrinfo("ollama.localhost", None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return {entry[4][0] for entry in addresses} == {"127.0.0.1"}


def _process_group():
    return ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True})


def _processes():
    if os.name == "nt":
        command = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                   "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine "
                   "| ConvertTo-Json -Compress")
        raw = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        rows = json.loads(raw.decode("utf-8", "replace"))
        return {row["ProcessId"]: row["CommandLine"] or ""
                for row in (rows if isinstance(rows, list) else [rows])}
    lines = subprocess.check_output(
        ["/bin/ps", "-axww", "-o", "pid=,command="], text=True, errors="replace", timeout=10,
    ).splitlines()
    return {int(pid): command for line in lines if len(parts := line.split(None, 1)) == 2
            for pid, command in [parts]}


def _owned_processes(env, venv):
    markers = (str(venv), env["HERMES_HOME"])

    def normalized(value):
        return value.replace("\\", "/").casefold() if os.name == "nt" else value

    matcher = re.compile(r'(?:^|[\s"=])(?:' + "|".join(re.escape(normalized(x)) for x in markers)
                         + r')(?=$|[\s"/])')
    return {pid: command for pid, command in _processes().items()
            if pid != os.getpid() and matcher.search(normalized(command))}


def _cleanup(process, env, venv, preexisting):
    if os.name == "nt":
        if process.poll() is None:
            with suppress(OSError):
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if process.poll() is None:
                process.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)

    # Hermes gateway stop can boot out launchd services. Scope cleanup to our
    # process group and exact canary-owned paths instead.
    def matching():
        return {pid: command for pid, command in _owned_processes(env, venv).items()
                if preexisting.get(pid) != command}

    leftovers = matching()
    for pid, command in leftovers.items():
        if matching().get(pid) != command:
            continue
        try:
            os.kill(pid, signal.SIGTERM if os.name == "nt" else signal.SIGKILL)
        except ProcessLookupError:
            pass
    if leftovers:
        for _ in range(8):
            if not (still_running := matching()):
                break
            time.sleep(0.1)
        else:
            raise OllamaError("hermes_cleanup_failed", f"canary Hermes processes remain: {list(still_running)}")
    return list(leftovers)


class HermesHarness:
    def __init__(self, root, tag, settings, guard, *, installer=_install, argv_factory=None):
        self.root = root
        self.tag = tag
        self.settings = settings
        _tag_version(tag)
        self.executable, self.metadata = installer(root, tag, guard)
        executable = Path(self.executable)
        if not executable.is_absolute() or not safe_path(root, executable).is_file():
            raise ValueError("Hermes executable must be inside the canary home")
        self.venv = executable.parent.parent
        self.argv = argv_factory(executable) if argv_factory else [str(executable)]
        work = _canary_dir(root, Path("attempts") / f"hermes-{tag}")
        self.bindir = executable.parent
        help_text = _run([*self.argv, "chat", "--help"], work, self._env(work), timeout=30)
        self.flags = detected_flags(help_text)
        self.supported = REQUIRED <= self.flags and bool({"--query-file", "-q"} & self.flags)
        self.metadata = {**self.metadata, "flags": sorted(self.flags)}

    def _env(self, work):
        env = hermes_env(self.root, self.tag, work)
        env["PATH"] = os.pathsep.join((str(self.bindir), env["PATH"]))
        return env

    def check(self, endpoint, model):
        attempts = [
            self.attempt(endpoint, model, attempt)
            for attempt in range(1, self.settings.repeats + 1)
        ]
        return {"check_id": "B6", "outcome": aggregate(item["outcome"] for item in attempts),
                "attempts": attempts}

    def attempt(self, endpoint, model, attempt):
        started = time.monotonic()
        evidence = {"argv": [], "exit_code": None, "events": [], "stderr_tail": [],
                    "file_bytes": None, "proxy_exchanges": [], "leftover_processes": [],
                    "hermes_sees_ollama": False, "attribution": "harness"}
        reason = self._execute(endpoint, model, evidence) if self.supported else "harness_cli_unsupported"
        if any(exchange["error"] for exchange in evidence["proxy_exchanges"]):
            evidence["attribution"] = "runtime"
        elif reason is None:
            evidence["attribution"] = None
        outcome = "timeout" if reason == "timeout_deadline" else "fail" if reason else "pass"
        return {"check_id": "B6", "attempt": attempt, "outcome": outcome, "reason": reason,
                "duration_s": time.monotonic() - started, **evidence}

    def _command(self, query, prompt, workdir, model, url):
        argv = [*self.argv, "chat", "--oneshot",
                "--provider", "custom", "--base-url", url, "-m", model, "-t", "file"]
        argv.extend(["--query-file", str(query)] if "--query-file" in self.flags else ["-q", prompt])
        optional = {
            "-Q": [], "--format": ["stream-json"], "--safe-mode": [], "--api-key": [API_KEY],
            "--reasoning": ["none"], "--max-turns": ["8"],
            "--run-budget": [str(max(1, int(self.settings.hermes_deadline_s - 30)))],
            "--yolo": [], "--source": ["tool"], "--in": [str(workdir)],
        }
        for flag, values in optional.items():
            if flag in self.flags:
                argv.extend([flag, *values])
        return argv

    def _execute(self, endpoint, model, evidence):
        parent = _canary_dir(self.root, Path("attempts") / f"hermes-{self.tag}")
        attempt_dir = safe_path(self.root, Path(tempfile.mkdtemp(dir=parent, prefix="b6-")))
        work = safe_path(self.root, attempt_dir / "work")
        work.mkdir()
        output = safe_path(self.root, work / "canary.txt")
        token = f"STACK-CANARY-{secrets.token_hex(4)}"
        prompt = (f"Use the write_file tool to create the file {output} containing exactly this "
                  f"text and nothing else: {token}. Do not use any other tool. Reply DONE when finished.")
        query = safe_path(self.root, attempt_dir / "query.txt")
        query.write_text(prompt, encoding="utf-8")
        env = self._env(work)
        preexisting = _owned_processes(env, self.venv)
        timed_out = False
        with RecordingProxy(endpoint, timeout=self.settings.stall_s) as proxy:
            evidence["hermes_sees_ollama"] = _local_ollama_name()
            host = "ollama.localhost" if evidence["hermes_sees_ollama"] else "127.0.0.1"
            argv = self._command(query, prompt, work, model, f"http://{host}:{proxy.server_port}/v1")
            evidence["argv"] = ["[redacted]" if arg == API_KEY else arg for arg in argv]
            stdout = safe_path(self.root, attempt_dir / "stdout.log")
            stderr = safe_path(self.root, attempt_dir / "stderr.log")
            with stdout.open("wb") as out, stderr.open("wb") as err:
                process = subprocess.Popen(argv, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=err, **_process_group())
                try:
                    process.wait(timeout=self.settings.hermes_deadline_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                finally:
                    evidence["leftover_processes"] = _cleanup(process, env, self.venv, preexisting)
                evidence["exit_code"] = process.returncode
        evidence["proxy_exchanges"] = proxy.exchanges
        with stderr.open(encoding="utf-8", errors="replace") as log:
            evidence["stderr_tail"] = [line.rstrip() for line in deque(log, maxlen=30)]
        if "--format" in self.flags:
            with stdout.open(encoding="utf-8", errors="replace") as log:
                for line in log:
                    try:
                        event = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if isinstance(event, dict):
                        evidence["events"].append(event)
        if not output.is_symlink() and output.is_file():
            with safe_path(self.root, output).open("rb") as file:
                content = file.read(65537)
            evidence["file_bytes"] = content.decode("utf-8", errors="surrogateescape")
        if timed_out:
            return "timeout_deadline"
        if evidence["file_bytes"] is None:
            return "file_missing"
        if evidence["file_bytes"] not in {token, token + "\n"}:
            return "file_mismatch"
        if evidence["exit_code"] != 0:
            return "harness_exit_nonzero"
        if "--format" in self.flags and not any(
            event.get("type") == "tool_use" and event.get("name") == "write_file"
            for event in evidence["events"]
        ):
            return "no_write_event"
        return None
