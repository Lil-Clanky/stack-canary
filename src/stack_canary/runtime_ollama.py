"""Install Ollama and run its isolated server and model stores."""

import ctypes
import hashlib
import os
import posixpath
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import deque
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from .config import Settings
from .releases import SHA256, Release, asset_filename, fetch_url, version_key
from .transport import request


class OllamaError(Exception):
    """An Ollama infrastructure failure with a stable result code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _reserved(path: Path) -> bool:
    user_home = Path.home()
    reserved = (user_home / ".ollama", user_home / ".hermes", user_home / ".local/bin/hermes",
                Path("/Applications"), Path("/usr/local/bin"))
    if sys.platform == "darwin":
        path = Path(str(path).casefold())
        reserved = tuple(Path(str(item).casefold()) for item in reserved)
    return any(path.is_relative_to(item) for item in reserved)


def safe_path(root: Path, p: Path) -> Path:
    """Resolve p under root and refuse paths that leave the canary home."""
    root = Path(os.path.abspath(root))
    target = root / p
    lexical = Path(os.path.abspath(target))
    if _reserved(root) or _reserved(lexical) or not lexical.is_relative_to(root):
        raise ValueError(f"path outside the canary home: {p}")
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if (_reserved(resolved_root) or _reserved(resolved_target)
            or not resolved_target.is_relative_to(resolved_root)):
        raise ValueError(f"path outside the canary home: {p}")
    return resolved_target


def canary_home() -> Path:
    home = Path(os.environ.get("CANARY_HOME") or Path.home() / ".stack-canary").expanduser()
    return safe_path(home, Path("."))


def _state_path(root: Path, relative: Path) -> Path:
    path = safe_path(root, relative)
    if path != root / relative:
        raise ValueError(f"managed state cannot use a symlink or junction: {relative}")
    return path


def _existing_parent(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    return path


def _tar_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
            or any(part.endswith((" ", ".")) or PureWindowsPath(part).is_reserved()
                   for part in path.parts)):
        raise ValueError(f"unsafe archive path: {name!r}")
    return path


def _tar_link(member: tarfile.TarInfo) -> PurePosixPath:
    name = member.linkname
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        raise ValueError(f"unsafe archive link: {member.name!r} -> {name!r}")
    base = posixpath.dirname(member.name) if member.issym() else ""
    relative = posixpath.join(base, name)
    _tar_path(posixpath.normpath(relative))
    return PurePosixPath(relative)


def _extract(archive: tarfile.TarFile, tree: Path, guard) -> None:
    members = archive.getmembers()
    for member in members:
        path = _tar_path(member.name)
        if not (member.isdir() or member.isreg() or member.issym() or member.islnk()):
            raise ValueError(f"unsupported archive member: {member.name!r}")
        if not path.parts and not member.isdir():
            raise ValueError("archive root must be a directory")
        if member.issym() or member.islnk():
            _tar_link(member)

    guard("extract runtime", sum(member.size for member in members if member.isreg()))
    data_filter = getattr(tarfile, "data_filter", None)
    archive.errorlevel = 2
    for member in members:
        destination = safe_path(tree, Path(member.name))
        if destination == tree:
            if not member.isdir():
                raise ValueError(f"unsafe archive destination: {member.name!r}")
            continue
        if member.issym() or member.islnk():
            safe_path(tree, Path(_tar_link(member)))
        if data_filter is not None:
            # extract() applies the data filter to the current filesystem state.
            archive.extract(member, tree, set_attrs=False, filter="data")
        else:
            if member.issym():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(member.linkname)
            elif member.islnk():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.hardlink_to(safe_path(tree, Path(_tar_link(member))))
            else:
                archive.extract(member, tree, set_attrs=False)
        if member.isreg():
            destination.chmod(0o700 | (member.mode & 0o055))
    for member in members:
        if member.issym() or member.islnk():
            safe_path(tree, Path(member.name))


def _binary(tree: Path) -> Path:
    for relative in (Path("ollama"), Path("bin/ollama")):
        candidate = safe_path(tree, relative)
        if candidate.is_file():
            return candidate
    raise ValueError("archive does not contain an ollama executable")


def _clone_file_function():
    # Apple's cp -c may silently copy on EXDEV or ENOTSUP. clonefile fails instead.
    clonefile = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).clonefile
    clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    clonefile.restype = ctypes.c_int

    def clone_file(src, dst):
        if clonefile(os.fsencode(src), os.fsencode(dst), 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), src, None, dst)

    return clone_file


def _clone_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, copy_function=_clone_file_function(), symlinks=True)


def _seed_read_path(root: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError(f"seed link escapes the source store: {path}")
    return resolved


def _raise_walk_error(error):
    raise error


def _validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535 or port in {9119, 9120, 11434}:
        raise ValueError("use a port from 1 to 65535 other than 9119, 9120 or 11434")


def _port_free(port: int) -> None:
    _validate_port(port)
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            probe.listen(1)
        except OSError as error:
            raise OllamaError("port_in_use", f"loopback port {port} is unavailable: {error}") from error


def _store(version: str, master: bool) -> Path:
    return Path("stores") / ("master" if master else f"ollama-{version}")


def derived_name(model: str) -> str:
    return "canary-" + model.replace(":", "-").replace("/", "-")


def model_key(model: str) -> str:
    return model if ":" in model.rsplit("/", 1)[-1] else model + ":latest"


def _server_overrides(version: str, port: int, settings: Settings, *, root=None, master=False) -> dict:
    """Build the canary-owned environment additions, without exposing inherited settings."""
    version_key(version)
    _validate_port(port)
    root = canary_home() if root is None else safe_path(Path(root), Path("."))
    home = _state_path(root, Path(f"homes/ollama-{version}"))
    models = _state_path(root, _store(version, master))
    temporary = _state_path(root, Path(f"homes/ollama-{version}/tmp"))
    env = {
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "OLLAMA_HOST": f"127.0.0.1:{port}",
        "OLLAMA_MODELS": str(models),
        "OLLAMA_NOPRUNE": "1",
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_CONTEXT_LENGTH": str(settings.context_length),
        "OLLAMA_FLASH_ATTENTION": "1",
        "OLLAMA_KV_CACHE_TYPE": settings.kv_cache_type,
        "OLLAMA_KEEP_ALIVE": "10m",
    }
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    return env


def server_env(version: str, port: int, settings: Settings, *, root=None, master=False) -> dict:
    return _clean_environment(_server_overrides(version, port, settings, root=root, master=master))


def _clean_environment(overrides):
    inherited = {key: value for key, value in os.environ.items() if not key.upper().startswith("OLLAMA_")}
    return {**inherited, **overrides}


class OllamaServer:
    """A single installed server process and its diagnostics."""

    def __init__(self, root, release, executable, settings, *, argv_factory=None,
                 master=False, ready_s=60, stop_s=15):
        self.root = root
        self.release = release
        self.settings = settings
        self.environment = _server_overrides(release.version, settings.port, settings, root=root, master=master)
        self.argv = argv_factory(executable) if argv_factory else [str(executable), "serve"]
        self.endpoint = f"http://{self.environment['OLLAMA_HOST']}"
        self.ready_s = ready_s
        self.stop_s = stop_s
        self.process = None
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        self.log_path = _state_path(root, Path(f"logs/ollama-{release.version}-{timestamp}.log"))
        self.log_tail = []
        self.log_matches = []

    def __enter__(self):
        _port_free(self.settings.port)
        for key in ("HOME", "TMPDIR", "OLLAMA_MODELS"):
            safe_path(self.root, Path(self.environment[key])).mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        process_group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt" else {"start_new_session": True}
        )
        with self.log_path.open("xb") as log:
            self.process = subprocess.Popen(
                self.argv, cwd=self.environment["HOME"], env=_clean_environment(self.environment),
                stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, **process_group,
            )
        try:
            self._ready()
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def _ready(self):
        deadline = time.monotonic() + self.ready_s
        while (remaining := deadline - time.monotonic()) > 0:
            if self.process.poll() is not None:
                raise OllamaError("server_exited", f"Ollama exited with status {self.process.returncode}")
            exchange = request(
                "GET", self.endpoint + "/api/version", None, deadline_s=min(remaining, 1), stall_s=0.5,
            )
            if not exchange.error and self.process.poll() is None:
                data = exchange.payload
                version = data.get("version") if isinstance(data, dict) else None
                if version != self.release.version:
                    raise OllamaError(
                        "version_mismatch", f"expected Ollama {self.release.version}, got {version!r}",
                    )
                return
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        raise OllamaError("readiness_timeout", f"Ollama did not become ready in {self.ready_s:g}s")

    def _stop(self):
        process = self.process
        if os.name == "nt":
            if process.poll() is None:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    process.terminate()
                try:
                    process.wait(timeout=self.stop_s)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + self.stop_s
            while True:
                process.poll()
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    break
                time.sleep(0.05)
            process.wait(timeout=5)

        deadline = time.monotonic() + 2
        while True:
            try:
                _port_free(self.settings.port)
                return
            except OllamaError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def __exit__(self, error_type, error, traceback):
        try:
            self._stop()
        finally:
            tail = deque(maxlen=30)
            with self.log_path.open(encoding="utf-8", errors="replace") as log:
                for line in log:
                    line = line.rstrip("\r\n")
                    tail.append(line)
                    if re.search(r"inference compute|offloaded|metal", line, re.IGNORECASE):
                        self.log_matches.append(line)
            self.log_tail = list(tail)


class OllamaRuntime:
    def __init__(self, root: Path, settings: Settings, *, fetch=fetch_url,
                 argv_factory=None, ready_s=60, stop_s=15):
        self.root = safe_path(root, Path("."))
        self.settings = settings
        self.fetch = fetch
        self.argv_factory = argv_factory
        self.ready_s = ready_s
        self.stop_s = stop_s

    def free_bytes(self):
        return shutil.disk_usage(_existing_parent(self.root)).free

    @contextmanager
    def exclusive(self):
        lock = _state_path(self.root, Path(".run-lock"))
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            lock.mkdir()
        except FileExistsError as error:
            raise OllamaError("home_in_use", f"canary home is locked: {lock}") from error
        try:
            yield
        finally:
            safe_path(self.root, lock).rmdir()

    def guard(self, operation: str, needed: int = 0) -> None:
        reserve = int(self.settings.min_free_gb * 1024 ** 3)
        free = self.free_bytes()
        if free < reserve + needed:
            raise OllamaError(
                "disk_space", f"{operation} needs {needed:,} bytes plus {reserve:,} bytes of free "
                f"space in reserve; {free:,} bytes available",
            )

    def tree(self, release: Release) -> Path:
        version_key(release.version)
        return _state_path(self.root, Path("runtimes/ollama") / release.version)

    def _download(self, release: Release, partial: Path) -> None:
        self.guard("download runtime")
        digest = hashlib.sha256()
        with self.fetch(release.asset_url, headers={"User-Agent": "stack-canary"}) as response:
            size = getattr(response, "headers", {}).get("Content-Length")
            if size and str(size).isdigit():
                self.guard("download runtime", int(size))
            with partial.open("xb") as file:
                while data := response.read(1024 * 1024):
                    self.guard("download runtime", len(data))
                    digest.update(data)
                    file.write(data)
        if digest.hexdigest() != release.sha256.lower():
            raise OllamaError("sha256_mismatch", f"v{release.version}: SHA-256 mismatch")

    def _publish(self, staged: Path, destination: Path, backup: Path) -> None:
        destination = safe_path(self.root, destination)
        backup = safe_path(self.root, backup)
        if destination.exists():
            destination.rename(backup)
        try:
            safe_path(self.root, staged).rename(safe_path(self.root, destination))
        except OSError:
            if backup.exists():
                safe_path(self.root, backup).rename(safe_path(self.root, destination))
            raise

    def install(self, release: Release) -> Path:
        tree = self.tree(release)
        if not SHA256.fullmatch(release.sha256):
            raise ValueError("release must have a SHA-256")
        asset_filename(release.asset_name)
        marker = _state_path(self.root, tree.relative_to(self.root) / ".complete")
        download = _state_path(
            self.root, Path("downloads") / release.version / release.asset_name,
        )
        partial = _state_path(self.root, download.relative_to(self.root).with_suffix(download.suffix + ".part"))
        if marker.is_file() and marker.read_text(encoding="ascii").strip() == release.sha256.lower():
            binary = _binary(tree)
            safe_path(self.root, download).unlink(missing_ok=True)
            safe_path(self.root, partial).unlink(missing_ok=True)
            return binary

        self.guard("install runtime")
        download.parent.mkdir(parents=True, exist_ok=True)
        tree.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=tree.parent))
        extracted = safe_path(self.root, staging / "tree")
        extracted.mkdir()
        try:
            safe_path(self.root, partial).unlink(missing_ok=True)
            self._download(release, partial)
            with tarfile.open(partial, "r:gz") as archive:
                _extract(archive, extracted, self.guard)
            binary = _binary(extracted)
            if sys.platform == "darwin":
                signed = subprocess.run(
                    ["/usr/bin/codesign", "-dv", str(binary)], stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, check=False,
                )
                _state_path(extracted, Path("codesign.txt")).write_bytes(signed.stdout)
            _state_path(extracted, Path(".complete")).write_text(release.sha256.lower() + "\n", encoding="ascii")
            self.guard("finish runtime extraction")
            self._publish(extracted, tree, staging / "previous")
            safe_path(self.root, download).unlink(missing_ok=True)
        finally:
            safe_path(self.root, partial).unlink(missing_ok=True)
            shutil.rmtree(safe_path(self.root, staging))
        return _binary(tree)

    def server(self, release: Release, *, master=False) -> OllamaServer:
        tree = self.tree(release)
        if safe_path(self.root, tree.parent / "lib/ollama").exists():
            raise ValueError("unexpected lib/ollama beside the version trees")
        return OllamaServer(
            self.root, release, _binary(tree), self.settings, argv_factory=self.argv_factory,
            master=master, ready_s=self.ready_s, stop_s=self.stop_s,
        )

    def api(self, endpoint, path, body=None, *, deadline_s=None) -> dict:
        exchange = request(
            "GET" if body is None else "POST", endpoint + path, body,
            deadline_s=self.settings.deadline_s if deadline_s is None else deadline_s,
            stall_s=self.settings.stall_s,
        )
        if exchange.error or not isinstance(exchange.payload, dict) or exchange.payload.get("error"):
            detail = exchange.error or (
                exchange.payload.get("error") if isinstance(exchange.payload, dict) else "expected a JSON object"
            )
            raise OllamaError("api_error", f"{path}: {detail}")
        return exchange.payload

    def _models(self, endpoint, path) -> list:
        models = self.api(endpoint, path).get("models")
        if not isinstance(models, list):
            raise OllamaError("api_error", f"{path}: expected a models array")
        return models

    def tags(self, endpoint) -> dict[str, str]:
        tags = {}
        for model in self._models(endpoint, "/api/tags"):
            if isinstance(model, dict) and isinstance(model.get("digest"), str):
                for key in ("name", "model"):
                    if isinstance(name := model.get(key), str):
                        tags[model_key(name)] = model["digest"]
        return tags

    def _pull(self, endpoint, model) -> None:
        self.guard(f"pull {model}")

        def progress(chunk):
            if not isinstance(chunk, dict):
                raise OllamaError("pull_failed", f"{model}: expected a pull progress object")
            if chunk.get("error"):
                raise OllamaError("pull_failed", f"{model}: {chunk['error']}")
            total, completed = chunk.get("total"), chunk.get("completed", 0)
            remaining = max(0, total - completed) if type(total) is type(completed) is int else 0
            self.guard(f"pull {model}", remaining)

        exchange = request(
            "POST", endpoint + "/api/pull", {"model": model, "stream": True},
            deadline_s=3600, stall_s=120, ndjson=True, on_line=progress,
        )
        if exchange.error or not exchange.chunks or exchange.chunks[-1].get("status") != "success":
            raise OllamaError("pull_failed", f"{model}: {exchange.error or 'pull did not complete'}")
        self.guard(f"finish pulling {model}")

    def seed_master(self, source: Path) -> set[str]:
        """Merge missing files by clone and return digests of imported manifests."""
        source = source.expanduser().resolve(strict=True)
        master = _state_path(self.root, _store("master", master=True))
        if not source.is_dir() or source.is_relative_to(master) or master.is_relative_to(source):
            raise ValueError("seed_store must be a separate model store directory")
        if sys.platform != "darwin":
            raise OllamaError("seed_clone_unavailable", "seed_store requires macOS clonefile")

        files = []
        for folder in ("blobs", "manifests", "manifests-v2"):
            start = source / folder
            if not os.path.lexists(start):
                continue
            if _seed_read_path(source, start) != start or not start.is_dir():
                raise ValueError(f"invalid seed store directory: {start}")
            for directory, subdirs, names in os.walk(start, onerror=_raise_walk_error):
                base = Path(directory)
                for name in subdirs:
                    child = base / name
                    if _seed_read_path(source, child) != child or child.is_symlink():
                        raise ValueError(f"seed store directory links are unsupported: {child}")
                for name in names:
                    child = base / name
                    target = _seed_read_path(source, child)
                    link = os.readlink(child) if child.is_symlink() else None
                    if not target.is_file() or (link is not None and Path(link).anchor):
                        raise ValueError(f"seed store requires regular files or relative file links: {child}")
                    files.append((child, target, link))

        self.guard("seed model store")
        imported = set()
        try:
            clone = _clone_file_function()
            for child, target, link in files:
                relative = child.relative_to(source)
                destination = master / relative
                parent = _state_path(self.root, destination.parent.relative_to(self.root))
                if os.path.lexists(destination):
                    safe_path(master, destination)
                    continue
                destination = safe_path(master, destination)
                parent.mkdir(parents=True, exist_ok=True)
                if link is None:
                    clone(child, destination)
                else:
                    safe_path(master, parent / link)
                    destination.symlink_to(link)
                if relative.parts[0] != "blobs":
                    imported.add(hashlib.sha256(target.read_bytes()).hexdigest())
        except OSError as error:
            raise OllamaError("seed_clone_failed", f"could not clone seed store: {error}") from error
        self.guard("finish seeding model store")
        return imported

    def prepare_master(self, models, release: Release, *, server=None) -> list[dict]:
        imported = self.seed_master(Path(self.settings.seed_store)) if self.settings.seed_store else set()
        server = self.server(release, master=True) if server is None else server
        with server:
            endpoint = server.endpoint
            tags = self.tags(endpoint)
            prepared = []
            for model in models:
                digest = tags.get(model_key(model))
                source = "present"
                if digest is None:
                    self._pull(endpoint, model)
                    tags = self.tags(endpoint)
                    source = "pulled"
                elif digest.removeprefix("sha256:") in imported:
                    source = "seeded"
                derived = derived_name(model)
                self.api(endpoint, "/api/create", {
                    "model": derived, "from": model,
                    "parameters": {"temperature": 0, "seed": 42}, "stream": False,
                })
                details = self.api(endpoint, "/api/show", {"model": model})
                info = details.get("model_info")
                context_length = None
                if isinstance(info, dict):
                    context_length = info.get(f"{info.get('general.architecture')}.context_length")
                    if context_length is None:
                        context_length = next(
                            (value for key, value in info.items() if key.endswith(".context_length")), None,
                        )
                capabilities = details.get("capabilities")
                prepared.append({
                    "name": model,
                    "source": source,
                    **({"seed_digest": digest} if source == "seeded" else {}),
                    "derived_model": derived,
                    "capabilities": capabilities if isinstance(capabilities, list) else [],
                    "requires": details.get("requires"),
                    "context_length": context_length,
                })
            tags = self.tags(endpoint)
            for model in prepared:
                model["digest"] = tags.get(model_key(model["name"]))
                model["derived_digest"] = tags.get(model_key(model["derived_model"]))
                if not model["digest"] or not model["derived_digest"]:
                    raise OllamaError("model_digest_missing", f"missing model digest for {model['name']}")
            return prepared

    def warm_up(self, endpoint, model: str, *, thinking: bool) -> dict:
        body = {
            "model": model, "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 1, "stream": False, "temperature": 0, "seed": 42,
        }
        if thinking:
            body["reasoning_effort"] = "none"
        self.api(endpoint, "/v1/chat/completions", body, deadline_s=600)
        for loaded in self._models(endpoint, "/api/ps"):
            if not isinstance(loaded, dict):
                continue
            if not any(
                isinstance(name := loaded.get(key), str) and model_key(name) == model_key(model)
                for key in ("name", "model")
            ):
                continue
            size, size_vram = loaded.get("size"), loaded.get("size_vram")
            if type(size) is not int or size <= 0 or type(size_vram) is not int or size_vram < 0:
                raise OllamaError("api_error", f"/api/ps: invalid GPU sizes for {model}")
            return {
                "size": size, "size_vram": size_vram, "offload": size_vram / size,
                "runner": loaded.get("runner"), "digest": loaded.get("digest"),
            }
        raise OllamaError("model_not_loaded", f"/api/ps does not list {model}")

    def clone_store(self, version: str) -> Path:
        version_key(version)
        master = _state_path(self.root, _store(version, master=True))
        destination = _state_path(self.root, _store(version, master=False))
        self.guard("clone model store")
        regular_bytes = 0
        for directory, subdirs, files in os.walk(master):
            for name in subdirs + files:
                item = Path(directory) / name
                target = safe_path(master, item)
                if item.is_symlink():
                    if Path(os.readlink(item)).anchor or not target.is_file():
                        raise ValueError(f"model store link must be relative and target a file: {item}")
                    continue
                if target != item:
                    raise ValueError(f"model stores cannot contain junctions: {item}")
                if name in files:
                    info = item.stat()
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError(f"model stores must contain regular files: {item}")
                    regular_bytes += info.st_size
        use_full_copy = sys.platform != "darwin" or self.settings.allow_full_copy
        if use_full_copy:
            self.guard("copy model store", regular_bytes)
        staging = Path(tempfile.mkdtemp(prefix=".clone-", dir=master.parent))
        clone = safe_path(self.root, staging / "store")
        try:
            if use_full_copy:
                shutil.copytree(master, clone, symlinks=True)
            else:
                try:
                    _clone_tree(master, clone)
                except OSError as error:
                    raise OllamaError("clone_failed", f"APFS clone failed: {error}") from error
            self.guard("finish model store clone")
            self._publish(clone, destination, staging / "previous")
        finally:
            shutil.rmtree(safe_path(self.root, staging))
        return destination
