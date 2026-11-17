"""Install tagged Hermes releases and run the isolated B6 file-write check."""

import ast
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

from . import hermes_foreground
from .outcome import aggregate
from .proxy import RecordingProxy, broken_completion
from .releases import _api_json, _stable_version, fetch_url, release_list, version_key
from .runtime_ollama import OllamaError, _state_path, safe_path

REPOSITORY = "https://github.com/NousResearch/hermes-agent.git"
API = "https://api.github.com/repos/NousResearch/hermes-agent/releases"
REQUIRED = {"--oneshot", "--provider", "--base-url", "-m", "-t"}
OPTIONAL = {
    "-Q", "--format", "--safe-mode", "--api-key", "--reasoning", "--max-turns",
    "--yolo", "--source", "--in",
}
GATEWAY_OPTIONS = REQUIRED | OPTIONAL | {"--query-file", "-q"}
CLI_REJECTION = re.compile(r"(?im)^\s*(?:error:\s*)?(?:unsupported\b[^\n]*\boptions\b|usage\s*:)")
API_KEY = "stack-canary-loopback"


def _tag_version(tag):
    if not tag.startswith("v"):
        raise ValueError(f"unsupported Hermes tag: {tag}")
    version = version_key(tag[1:])
    if version[0] >= 2000 or version < (0, 21, 5):
        raise ValueError(f"unsupported Hermes tag: {tag}")
    return version


def hermes_tags(spec, *, fetch=fetch_url, cache_root=None, read_only=False):
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
    listing = release_list(API, fetch, cache_root=cache_root, cache_name="hermes.json", read_only=read_only)
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


def hermes_env(root, tag, workdir, *, profile=None):
    _tag_version(tag)
    hermes_home = _canary_dir(root, profile if profile is not None else Path("homes") / f"hermes-{tag}")
    # Hermes repairs HOME=<profile>/home back to the OS account for subprocesses.
    home = _canary_dir(root, hermes_home.relative_to(root) / "os-home")
    tmp = _canary_dir(root, home.relative_to(root) / "tmp")
    system_path = os.defpath
    if os.name == "nt":
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        system_path = os.pathsep.join((str(windows / "System32"), str(windows)))
    return {
        **_clean_env(home, tmp),
        "PATH": system_path,
        "HERMES_HOME": str(hermes_home),
        "HERMES_REAL_HOME": str(home),
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


def _python_minor(root, src):
    version_file = safe_path(root, src / ".python-version")
    lock = safe_path(root, src / "pm/lock.json")
    pins = []
    if version_file.is_file():
        pins.append(version_file.read_text(encoding="utf-8").strip())
    if lock.is_file():
        pins.append(json.loads(lock.read_text(encoding="utf-8")).get("packages", {}).get("python", {}).get("version"))
    for pin in pins:
        if isinstance(pin, str) and (match := re.fullmatch(r"(3\.\d+)(?:\.\d+)?(?:\+\d+)?", pin)):
            return match[1]
    return "3.13"


def _matching_python(path, minor, src, env):
    try:
        version = _run([str(path), "--version"], src, env, timeout=10)
    except (OllamaError, OSError, subprocess.SubprocessError):
        return False
    return re.match(rf"Python {re.escape(minor)}(?:[.\s]|$)", version) is not None


def _interpreter(minor, src, env, uv):
    paths = dict.fromkeys(shutil.which(name) for name in (f"python{minor}", "python3", "python"))
    for path in paths:
        if path:
            absolute = Path(path).absolute()
            if _matching_python(absolute, minor, src, env):
                return str(absolute)
    if not uv:
        raise OllamaError("hermes_setup_failed", f"Python {minor} is not on PATH and uv is unavailable")
    try:
        _run([uv, "python", "install", "--no-bin", "--no-registry", minor], src, env, timeout=600)
        found = Path(_run([uv, "python", "find", "--managed-python", "--no-project", minor],
                          src, env, timeout=30))
        directory = Path(env["UV_PYTHON_INSTALL_DIR"])
        if found.is_absolute() and safe_path(directory, found).is_file() and _matching_python(found, minor, src, env):
            return str(found)
        raise ValueError("uv did not find a matching interpreter in the canary Python directory")
    except (OllamaError, OSError, ValueError, subprocess.SubprocessError) as error:
        raise OllamaError("hermes_setup_failed", f"could not find or install Python {minor}: {error}") from error


def _dependencies(root, src, venv, env):
    with safe_path(root, src / "pyproject.toml").open("rb") as file:
        project = tomllib.load(file)["project"]
    extra = "all" if "all" in project.get("optional-dependencies", {}) else None
    requirement = str(src) + (f"[{extra}]" if extra else "")

    python = _bin(venv, "python")
    if uv := shutil.which("uv"):
        env.update({
            "UV_CACHE_DIR": str(_canary_dir(root, Path("uv-cache"))), "UV_LINK_MODE": "clone",
            "UV_PYTHON_INSTALL_DIR": str(_canary_dir(root, Path("uv-python"))),
            "UV_PROJECT_ENVIRONMENT": str(venv),
        })
    interpreter = _interpreter(_python_minor(root, src), src, env, uv)
    if uv:
        if safe_path(root, src / "uv.lock").is_file():
            args = [uv, "sync", "--frozen", "--no-default-groups", "--python", interpreter]
            if extra:
                args.extend(["--extra", extra])
            _run(args, src, env, timeout=1200)
            return "uv_sync_editable"
        _run([uv, "venv", "--python", interpreter, str(venv)], src, env, timeout=600)
        _run([uv, "pip", "install", "--python", str(python), "-e", requirement], src, env, timeout=1200)
        return "uv_pip_editable"

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
            try:
                version = _run([str(python), "--version"], prefix, env, timeout=10)
            except (OllamaError, OSError, subprocess.SubprocessError):
                pass
            else:
                if version == metadata.get("python_version"):
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


def _gateway_options(root, source):
    """Apply the checked-out gateway's refusal list without importing Hermes."""
    source = safe_path(root, source)
    if not source.is_file():
        return GATEWAY_OPTIONS
    aliases = {"model": "-m", "toolsets": "-t", "quiet": "-Q", "query": "-q",
               "in_dir": "--in", "oneshot_exit": "--oneshot", "output_format": "--format"}
    for node in ast.parse(source.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_UNSUPPORTED" for target in node.targets
        ):
            names = ast.literal_eval(node.value)
            blocked = {("-" if len(name) == 1 else "--") + name.replace("_", "-") for name in names}
            blocked.update(aliases[name] for name in names if name in aliases)
            return GATEWAY_OPTIONS - blocked
    return GATEWAY_OPTIONS


def detected_flags(help_text, gateway_options=GATEWAY_OPTIONS):
    flags = set(re.findall(r"(?<![\w-])(?:--[a-z][a-z0-9-]*|-[A-Za-z])(?![\w-])", help_text))
    flags.intersection_update(gateway_options)
    if "stream-json" not in help_text:
        flags.discard("--format")
    return flags


def invocation_rejected(evidence):
    if evidence.get("proxy_exchanges"):
        return False
    if evidence.get("exit_code") == 2 or evidence.get("cli_diagnostic"):
        return True
    messages = [*(evidence.get("stderr_tail") or []),
                *(event.get("error") for event in evidence.get("events") or [])]
    return any(isinstance(message, str) and CLI_REJECTION.search(message) for message in messages)


def classify_attempt(attempt):
    """Apply current attribution rules to live and saved evidence."""
    if attempt["outcome"] == "pass":
        return {**attempt, "attribution": None}
    if invocation_rejected(attempt):
        return {**attempt, "outcome": "error", "reason": "harness_cli_rejected", "attribution": "canary"}
    if "proxy_exchanges" not in attempt:
        return attempt
    exchanges = attempt["proxy_exchanges"] or []
    if not exchanges:
        reason = attempt.get("reason")
        if reason not in {"harness_cli_unsupported", "harness_requires_launchd", "harness_cli_rejected"}:
            reason = "harness_not_started"
        return {**attempt, "outcome": "error", "reason": reason, "attribution": "harness",
                "note": "before_first_request"}
    return {**attempt, "attribution": "runtime" if any(map(broken_completion, exchanges)) else "harness"}


def b6_summary(attempts):
    outcome = aggregate(item["outcome"] for item in attempts)
    if outcome == "pass":
        return {"outcome": outcome, "attribution": None}
    if outcome == "error":
        errors = [item for item in attempts if item["outcome"] == "error"]
        primary = next((item for item in errors if item.get("attribution") == "canary"), errors[0])
        return {"outcome": outcome, "reason": primary.get("reason"), "attribution": primary.get("attribution")}
    attribution = "runtime" if any(item.get("attribution") == "runtime" for item in attempts) else "harness"
    return {"outcome": outcome, "attribution": attribution}


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
        process.poll()
        with suppress(ProcessLookupError, PermissionError):
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
    def __init__(self, root, tag, settings, guard, *, installer=_install, argv_factory=None, probe=None):
        from .hermes_registry import verified_commit

        self.root = root
        self.tag = tag
        self.settings = settings
        self.probe = probe
        _tag_version(tag)
        macos = sys.platform == "darwin" and argv_factory is None
        expected = verified_commit(root, tag) if macos and probe is None else None
        self.disabled = macos and probe is None and expected is None
        if self.disabled:
            self._disable({"tag": tag})
            return
        self.executable, self.metadata = installer(root, tag, guard)
        if macos and probe is None and self.metadata.get("commit") != expected:
            self._disable(self.metadata)
            return
        self.foreground = macos
        executable = Path(self.executable)
        if not executable.is_absolute() or not safe_path(root, executable).is_file():
            raise ValueError("Hermes executable must be inside the canary home")
        self.venv = executable.parent.parent
        self.argv = argv_factory(executable) if argv_factory else [str(executable)]
        work = _canary_dir(root, Path("attempts") / f"hermes-{tag}")
        self.bindir = executable.parent
        help_text = _run(self._protected([*self.argv, "chat", "--help"]), work, self._env(work), timeout=30)
        source = self.venv.parent / "src/hermes_cli/gateway_chat.py"
        self.flags = detected_flags(help_text, _gateway_options(root, source))
        self.supported = REQUIRED <= self.flags and bool({"--query-file", "-q"} & self.flags)
        self.metadata = {**self.metadata, "flags": sorted(self.flags),
                         "gateway_mode": "foreground" if self.foreground else "default_gateway"}

    def _disable(self, metadata):
        self.disabled = True
        self.metadata = {**metadata, "gateway_mode": "disabled", "reason": "harness_requires_launchd"}
        print(f"canary: Hermes {self.tag}: B6 disabled on macOS (harness_requires_launchd)", file=sys.stderr)

    def _env(self, work, *, profile=None):
        env = hermes_env(self.root, self.tag, work, profile=profile)
        env["PATH"] = os.pathsep.join((str(self.bindir), env["PATH"]))
        if self.probe is not None:
            env["PATH"] = os.pathsep.join((str(self.probe.parent), env["PATH"]))
            env["STACK_CANARY_LAUNCHCTL_LOG"] = str(self.probe)
        if profile is not None:
            env["HERMES_GATEWAY_LOCK_DIR"] = str(Path(env["HERMES_HOME"]) / "locks")
        return env

    def _protected(self, argv):
        if self.probe is None:
            return argv
        # PATH catches cooperative calls; Seatbelt also blocks the absolute system executable.
        profile = ('(version 1) (allow default) '
                   '(deny process-exec (literal "/bin/launchctl") (literal "/usr/bin/launchctl")) '
                   '(deny file-write* (subpath {hermes}) (subpath {ollama}) (subpath {agents}))')
        home = Path.home()
        return ["/usr/bin/sandbox-exec", "-p", profile.format(
            hermes=json.dumps(str(home / ".hermes")), ollama=json.dumps(str(home / ".ollama")),
            agents=json.dumps(str(home / "Library/LaunchAgents"))), *argv]

    def check(self, endpoint, model):
        if self.disabled:
            return {"check_id": "B6", "outcome": "error", "reason": "harness_requires_launchd",
                    "attribution": "harness", "attempts": []}
        attempts = [
            self.attempt(endpoint, model, attempt)
            for attempt in range(1, self.settings.repeats + 1)
        ]
        return {"check_id": "B6", "attempts": attempts, **b6_summary(attempts)}

    def attempt(self, endpoint, model, attempt):
        started = time.monotonic()
        evidence = {"argv": [], "exit_code": None, "events": [], "stderr_tail": [],
                    "file_bytes": None, "proxy_exchanges": [], "leftover_processes": [],
                    "hermes_sees_ollama": False}
        reason = self._execute(endpoint, model, evidence) if self.supported else "harness_cli_unsupported"
        outcome = "timeout" if reason == "timeout_deadline" else "fail" if reason else "pass"
        return classify_attempt({"check_id": "B6", "attempt": attempt, "outcome": outcome, "reason": reason,
                                 "duration_s": time.monotonic() - started, **evidence})

    def _command(self, query, prompt, workdir, model, url):
        argv = [*self.argv, "chat", "--oneshot",
                "--provider", "custom", "--base-url", url, "-m", model, "-t", "file"]
        argv.extend(["--query-file", str(query)] if "--query-file" in self.flags else ["-q", prompt])
        optional = {
            "-Q": [], "--format": ["stream-json"], "--safe-mode": [], "--api-key": [API_KEY],
            "--reasoning": ["none"], "--max-turns": ["8"],
            "--yolo": [], "--source": ["tool"], "--in": [str(workdir)],
        }
        for flag, values in optional.items():
            if flag in self.flags:
                argv.extend([flag, *values])
        return argv

    def _execute(self, endpoint, model, evidence):
        relative = Path("b6") if self.foreground else Path("attempts") / f"hermes-{self.tag}"
        parent = _canary_dir(self.root, relative)
        attempt_dir = safe_path(self.root, Path(tempfile.mkdtemp(dir=parent, prefix="b6-")))
        work = safe_path(self.root, attempt_dir / "work")
        work.mkdir()
        output = safe_path(self.root, work / "canary.txt")
        token = f"STACK-CANARY-{secrets.token_hex(4)}"
        prompt = (f"Use the write_file tool to create the file {output} containing exactly this "
                  f"text and nothing else: {token}. Do not use any other tool. Reply DONE when finished.")
        query = safe_path(self.root, attempt_dir / "query.txt")
        query.write_text(prompt, encoding="utf-8")
        profile = attempt_dir.relative_to(self.root) / "h" if self.foreground else None
        env = self._env(work, profile=profile)
        preexisting = _owned_processes(env, self.venv)
        timed_out = False
        with RecordingProxy(endpoint, timeout=self.settings.stall_s) as proxy:
            evidence["hermes_sees_ollama"] = _local_ollama_name()
            host = "ollama.localhost" if evidence["hermes_sees_ollama"] else "127.0.0.1"
            argv = self._command(query, prompt, work, model, f"http://{host}:{proxy.server_port}/v1")
            evidence["argv"] = ["[redacted]" if arg == API_KEY else arg for arg in argv]
            if self.foreground:
                gateway_log = safe_path(self.root, attempt_dir / "gateway.log")
                evidence["gateway_log"] = str(gateway_log)
                evidence["gateway_argv"] = [*self.argv, *hermes_foreground.GATEWAY_ARGS]
                # A POSIX venv's interpreter may link to the base interpreter.
                # Execute through the venv path so Python loads its pyvenv.cfg.
                python = _bin(self.venv, "python")
                argv = [str(python), str(Path(hermes_foreground.__file__).resolve()),
                        str(min(60, self.settings.hermes_deadline_s)), str(gateway_log), *argv]
            stdout = safe_path(self.root, attempt_dir / "stdout.log")
            stderr = safe_path(self.root, attempt_dir / "stderr.log")
            with stdout.open("wb") as out, stderr.open("wb") as err:
                process = subprocess.Popen(self._protected(argv), cwd=work, env=env, stdin=subprocess.DEVNULL,
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
        if not evidence["proxy_exchanges"]:
            for path in (stderr, stdout):
                with path.open(encoding="utf-8", errors="replace") as log:
                    diagnostic = next((line.strip() for line in log if CLI_REJECTION.search(line)), None)
                if diagnostic:
                    evidence["cli_diagnostic"] = diagnostic
                    break
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
