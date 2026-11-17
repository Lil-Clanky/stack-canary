import errno
import hashlib
import io
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
from contextlib import closing
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from fake_ollama import FakeOllama
from stack_canary import managed, runtime_ollama
from stack_canary.cli import main
from stack_canary.config import Settings
from stack_canary.releases import API_URL, DEFAULT_ASSET, Release
from stack_canary.runtime_ollama import (
    OllamaError, OllamaRuntime, safe_path, server_env,
)

FAKE_SCRIPT = Path(__file__).with_name("fake_ollama.py").resolve()


def make_tarball(**extra):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in {"ollama": b"fake executable", "lib/ollama/fixture": b"library", **extra}.items():
            member = tarfile.TarInfo(name)
            if isinstance(data, bytes):
                member.size = len(data)
                member.mode = 0o755
                archive.addfile(member, io.BytesIO(data))
            else:
                member.type, member.linkname = data
                archive.addfile(member)
    return stream.getvalue()


def fake_argv(binary, *, version=None, mode="ok", requires="0.0.0"):
    environment = {
        "FAKE_OLLAMA_VERSION": version or binary.parent.name,
        "FAKE_OLLAMA_MODE": mode,
        "FAKE_OLLAMA_REQUIRES": requires,
    }
    launcher = (
        f"import os, runpy, sys; os.environ.update({environment!r}); "
        f"sys.argv = [{str(FAKE_SCRIPT)!r}, 'serve']; "
        f"runpy.run_path({str(FAKE_SCRIPT)!r}, run_name='__main__')"
    )
    return [sys.executable, "-B", "-c", launcher]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path.resolve() / "canary-home"
    monkeypatch.setenv("CANARY_HOME", str(path))
    return path


@pytest.fixture
def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def settings(port):
    return Settings(port=port, min_free_gb=0, allow_full_copy=True, deadline_s=2, stall_s=0.6)


def fixture_release(data, version="1.10.0"):
    url = f"https://example.invalid/{version}/{DEFAULT_ASSET}"
    return Release(version, f"v{version}", DEFAULT_ASSET, url, hashlib.sha256(data).hexdigest())


def test_safe_path(home):
    assert safe_path(home, Path("stores/ok")) == home / "stores/ok"
    for path in (Path("../outside"), home.parent, Path.home() / ".ollama"):
        with pytest.raises(ValueError, match="outside"):
            safe_path(home, path)


def test_safe_path_refuses_link_to_outside(home):
    home.mkdir()
    outside = home.parent / "outside"
    outside.mkdir()
    try:
        (home / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi

        _winapi.CreateJunction(str(outside), str(home / "link"))
    with pytest.raises(ValueError, match="outside"):
        safe_path(home, Path("link/content"))


def test_install_is_verified_and_idempotent(home, settings):
    data = make_tarball()
    release = fixture_release(data)
    calls = []

    def fetch(url, *, headers):
        calls.append(url)
        return io.BytesIO(data)

    runtime = OllamaRuntime(home, settings, fetch=fetch)
    executable = runtime.install(release)
    assert executable == home / "runtimes/ollama/1.10.0/ollama"
    assert (executable.parent / ".complete").read_text().strip() == release.sha256
    assert (executable.parent / "lib/ollama/fixture").read_bytes() == b"library"
    assert not (home / "downloads/1.10.0" / DEFAULT_ASSET).exists()
    assert runtime.install(release) == executable
    assert calls == [release.asset_url]


def test_seed_merges_without_overwriting_or_writing_source(home, settings, monkeypatch):
    source = home.parent / "seed"
    blob = source / "blobs/sha256-test"
    manifest = source / "manifests/registry.ollama.ai/library/x/latest"
    for path, content in ((blob, b"source blob"), (manifest, b"source manifest")):
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    target = home / "stores/master/blobs/sha256-test"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing blob")

    def fingerprint():
        return {
            path.relative_to(source): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in source.rglob("*") if path.is_file()
        }

    before = fingerprint()
    monkeypatch.setattr(runtime_ollama, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(runtime_ollama, "_clone_file_function", lambda: shutil.copy2)
    runtime = OllamaRuntime(home, settings)
    assert runtime.seed_master(source) == {hashlib.sha256(b"source manifest").hexdigest()}
    assert runtime.seed_master(source) == set()
    assert target.read_bytes() == b"existing blob"
    assert (home / "stores/master" / manifest.relative_to(source)).read_bytes() == b"source manifest"
    assert fingerprint() == before


def test_seed_keeps_internal_symlinks_and_rejects_escapes(home, settings, monkeypatch):
    source = home.parent / "seed"
    (source / "blobs").mkdir(parents=True)
    (source / "blobs/digest").write_bytes(b"manifest")
    (source / "manifests-v2").mkdir()
    link = source / "manifests-v2/tag"
    try:
        link.symlink_to("../blobs/digest")
    except OSError:
        if os.name == "nt":
            pytest.skip("file symlinks require Windows symlink privileges")
        raise
    monkeypatch.setattr(runtime_ollama, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(runtime_ollama, "_clone_file_function", lambda: shutil.copy2)
    runtime = OllamaRuntime(home, settings)
    runtime.seed_master(source)
    seeded = home / "stores/master/manifests-v2/tag"
    assert os.readlink(seeded) == "../blobs/digest"
    assert seeded.read_bytes() == b"manifest"
    outside = home.parent / "outside"
    outside.write_bytes(b"untouched")
    (source / "manifests-v2/escape").symlink_to("../../outside")
    with pytest.raises(ValueError, match="escapes the source"):
        runtime.seed_master(source)
    assert outside.read_bytes() == b"untouched"


@pytest.mark.parametrize("extra", [
    {"../outside": b"rejected"},
    {"/absolute": b"rejected"},
    {"C:/absolute": b"rejected"},
    {"link": (tarfile.SYMTYPE, "../../outside")},
    {"link": (tarfile.LNKTYPE, "../../outside")},
    {"fifo": (tarfile.FIFOTYPE, "")},
    {"lib/nUl.txt": b"rejected"},
    {"lib/COM¹.ext": b"rejected"},
    {"lib/AUX .cfg": b"rejected"},
])
def test_unsafe_tar_leaves_no_archive_or_tree(home, settings, extra):
    data = make_tarball(**extra)
    runtime = OllamaRuntime(home, settings, fetch=lambda *args, **kwargs: io.BytesIO(data))
    with pytest.raises(ValueError, match="archive"):
        runtime.install(fixture_release(data))
    assert not any(path.is_file() for path in home.rglob("*"))
    assert not list(home.rglob(".install-*"))


def test_bad_sha_leaves_no_archive_or_tree(home, settings):
    data = make_tarball()
    runtime = OllamaRuntime(home, settings, fetch=lambda *args, **kwargs: io.BytesIO(data))
    with pytest.raises(OllamaError, match="SHA-256 mismatch"):
        runtime.install(replace(fixture_release(data), sha256="a" * 64))
    assert not any(path.is_file() for path in home.rglob("*"))
    assert not list(home.rglob(".install-*"))


def test_invalid_reinstall_preserves_existing_tree(home, settings):
    data = make_tarball()
    broken = make_tarball(**{"../outside": b"bad"})
    runtime = OllamaRuntime(home, settings, fetch=lambda *args, **kwargs: io.BytesIO(data))
    executable = runtime.install(fixture_release(data))
    runtime.fetch = lambda *args, **kwargs: io.BytesIO(broken)
    with pytest.raises(ValueError, match="archive"):
        runtime.install(fixture_release(broken))
    assert executable.read_bytes() == b"fake executable"
    assert (executable.parent / ".complete").read_text().strip() == fixture_release(data).sha256


def test_fails_disk_guard_before_download(home, settings, monkeypatch):
    runtime = OllamaRuntime(home, replace(settings, min_free_gb=5),
                            fetch=lambda *a, **kw: pytest.fail("download started"))
    monkeypatch.setattr(runtime, "free_bytes", lambda: 4 * 1024 ** 3)
    with pytest.raises(OllamaError, match="reserve") as failure:
        runtime.install(fixture_release(b"unused"))
    assert failure.value.code == "disk_space"
    assert not home.exists()


def test_server_env_scrubs_ollama_and_uses_owned_paths(home, settings, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", "outside-stores")
    monkeypatch.setenv("OLLAMA_CONTEXT_LENGTH", "2048")
    monkeypatch.setenv("OLLAMA_INHERITED_ONLY", "remove")
    monkeypatch.setenv("CANARY_TEST_INHERITED", "keep")
    env = server_env("1.10.0", settings.port, settings)
    assert "OLLAMA_INHERITED_ONLY" not in env
    assert env["CANARY_TEST_INHERITED"] == "keep"
    assert Path(env["HOME"]) == home / "homes/ollama-1.10.0"
    assert Path(env["OLLAMA_MODELS"]) == home / "stores/ollama-1.10.0"
    assert env["OLLAMA_CONTEXT_LENGTH"] == "65536"
    assert env["OLLAMA_NOPRUNE"] == "1"
    assert env["OLLAMA_KEEP_ALIVE"] == "10m"
    with pytest.raises(ValueError, match="11434"):
        server_env("1.10.0", 11434, settings)


def installed_runtime(home, settings, argv_factory=fake_argv):
    data = make_tarball()
    release = fixture_release(data)
    runtime = OllamaRuntime(home, settings, fetch=lambda *args, **kwargs: io.BytesIO(data),
                            argv_factory=argv_factory, ready_s=4, stop_s=0.5)
    runtime.install(release)
    return runtime, release


def test_refuses_an_occupied_port(home, settings):
    with FakeOllama() as existing:
        runtime, release = installed_runtime(home, replace(settings, port=existing.server_port))
        server = runtime.server(release)
        with pytest.raises(OllamaError, match="unavailable") as failure, server:
            pytest.fail("started over existing server")
        assert failure.value.code == "port_in_use"
        assert server.process is None


def test_readiness_mismatch_stops_server(home, settings):
    runtime, release = installed_runtime(home, settings, partial(fake_argv, version="9.9.9"))
    server = runtime.server(release)
    with pytest.raises(OllamaError, match="9.9.9") as failure, server:
        pytest.fail("invalid version became ready")
    assert failure.value.code == "version_mismatch"
    assert server.process.poll() is not None


def test_context_stops_process_and_frees_port(home, settings):
    runtime, release = installed_runtime(home, settings)
    with runtime.server(release) as server:
        assert server.process.poll() is None
    assert server.process.poll() is not None
    with FakeOllama(settings.port):
        pass


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
@pytest.mark.parametrize("denied_signal", [0, signal.SIGTERM])
def test_stop_tolerates_killpg_permission_error(home, settings, monkeypatch, denied_signal):
    runtime, release = installed_runtime(home, settings)
    server = runtime.server(release)
    calls = []
    server.process = SimpleNamespace(
        pid=123, poll=lambda: calls.append("poll"), wait=lambda *, timeout: calls.append(("wait", timeout)),
    )

    def killpg(pid, signum):
        assert pid == server.process.pid
        calls.append(signum)
        if signum == denied_signal:
            raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(runtime_ollama.os, "killpg", killpg)
    server._stop()
    expected = ["poll", signal.SIGTERM]
    if denied_signal == 0:
        expected.extend(["poll", 0])
    assert calls == [*expected, ("wait", 5)]
    assert len(server.stop_notes) == 1
    assert "Operation not permitted" in server.stop_notes[0]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX exit notifications without reaping")
def test_stop_reaps_an_already_exited_posix_child(home, settings):
    runtime, release = installed_runtime(home, settings)
    server = runtime.server(release)
    with subprocess.Popen(
        [sys.executable, "-B", "-c", "import sys; sys.stdin.buffer.read(1); sys.exit(7)"],
        cwd=home, stdin=subprocess.PIPE, start_new_session=True,
    ) as child:
        # Wait for exit without collecting its status. Darwin supplies kqueue.
        if hasattr(select, "kqueue"):
            with closing(select.kqueue()) as queue:
                event = select.kevent(child.pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD,
                                      fflags=select.KQ_NOTE_EXIT)
                queue.control([event], 0)
                child.stdin.close()
                assert queue.control(None, 1, 5)
        else:
            child.stdin.close()
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert child.returncode is None
        server.process = child
        server._stop()
        assert child.returncode == 7


def test_stop_raises_when_port_remains_busy(home, settings, monkeypatch):
    with FakeOllama() as listener:
        runtime, release = installed_runtime(home, replace(settings, port=listener.server_port))
        server = runtime.server(release)
        server.process = SimpleNamespace(poll=lambda: 0, wait=lambda *, timeout: 0)
        monkeypatch.setattr(server, "_signal_group", lambda _: False)
        ticks = iter([0, 3] if os.name == "nt" else [0, 0, 3])
        monkeypatch.setattr(runtime_ollama, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
        with pytest.raises(OllamaError, match=f"did not release port {listener.server_port}") as failure:
            server._stop()
    assert failure.value.code == "stop_failed"


@pytest.mark.parametrize("original_failure", ["startup", "body", None])
def test_context_preserves_original_failure_during_cleanup(home, settings, monkeypatch, original_failure):
    runtime, release = installed_runtime(home, settings)
    server = runtime.server(release)
    original = OllamaError("api_error", "/v1/chat/completions: connection_error")
    shutdown = OllamaError("stop_failed", "port stayed busy")

    def start():
        server.process = object()
        if original_failure == "startup":
            raise original

    def stop():
        raise shutdown

    monkeypatch.setattr(server, "_start", start)
    monkeypatch.setattr(server, "_stop", stop)
    server.log_path.parent.mkdir(parents=True)
    server.log_path.write_text("fixture log\n", encoding="utf-8")
    with pytest.raises(OllamaError) as failure, server:
        if original_failure == "body":
            raise original
    assert failure.value is (original if original_failure else shutdown)
    assert server.log_tail == ["fixture log"]
    assert server.stop_notes == ["Ollama shutdown failed: port stayed busy"]
    if original_failure:
        assert original.__notes__ == server.stop_notes


def test_clone_fallback_replaces_without_sharing(home, settings):
    runtime = OllamaRuntime(home, settings)
    master = home / "stores/master"
    master.mkdir(parents=True)
    (master / "manifest").write_text("master")
    stale = home / "stores/ollama-1.10.0"
    stale.mkdir()
    (stale / "stale").touch()
    clone = runtime.clone_store("1.10.0")
    assert clone == stale
    assert not (clone / "stale").exists()
    (clone / "manifest").write_text("changed")
    assert (master / "manifest").read_text() == "master"
    assert not list((home / "stores").glob(".clone-*"))


def test_clone_preserves_internal_manifest_link(home, settings):
    master = home / "stores/master"
    manifest = master / "manifests-v2/ollama.com/library/x/latest"
    blob = master / "blobs/sha256-test"
    manifest.parent.mkdir(parents=True)
    blob.parent.mkdir()
    blob.write_text("master")
    try:
        manifest.symlink_to("../../../../blobs/sha256-test")
    except OSError:
        if os.name == "nt":
            pytest.skip("file symlinks require Windows symlink privileges")
        raise

    clone = OllamaRuntime(home, settings).clone_store("1.10.0")
    cloned_manifest = clone / manifest.relative_to(master)
    cloned_blob = clone / blob.relative_to(master)
    assert cloned_manifest.is_symlink()
    assert cloned_manifest.resolve() == cloned_blob
    cloned_blob.write_text("clone")
    assert cloned_manifest.read_text() == "clone"
    assert manifest.read_text() == "master"


def test_clone_refuses_store_link_to_sibling(home, settings):
    master = home / "stores/master"
    sibling = master.parent / "sibling"
    master.mkdir(parents=True)
    sibling.mkdir()
    link = master / "outside"
    try:
        link.symlink_to("../sibling", target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi

        _winapi.CreateJunction(str(sibling), str(link))
    with pytest.raises(ValueError, match="outside the canary home"):
        OllamaRuntime(home, settings).clone_store("1.10.0")
    assert not (master.parent / "ollama-1.10.0").exists()


def test_apfs_failure_does_not_fall_back(home, settings, monkeypatch):
    runtime = OllamaRuntime(home, replace(settings, allow_full_copy=False))
    (home / "stores/master").mkdir(parents=True)
    monkeypatch.setattr(runtime_ollama, "sys", SimpleNamespace(platform="darwin"))
    def fail_clone(source, destination):
        raise OSError("fixture clone failure")

    monkeypatch.setattr(runtime_ollama, "_clone_tree", fail_clone)
    monkeypatch.setattr(runtime_ollama.shutil, "copytree", lambda *args: pytest.fail("full copy attempted"))
    with pytest.raises(OllamaError, match="APFS clone failed"):
        runtime.clone_store("1.10.0")
    assert not (home / "stores/ollama-1.10.0").exists()
    assert not list(home.glob("stores/.clone-*"))


def managed_fixture(monkeypatch, settings, *, versions=("1.10.0",), argv_factory=fake_argv):
    data = make_tarball()
    releases = [fixture_release(data, version=version) for version in versions]
    calls = []

    def fetch(url, *, headers):
        calls.append(url)
        if url == API_URL + "?per_page=50":
            values = [
                {"tag_name": release.tag, "assets": [{
                    "name": release.asset_name, "digest": "sha256:" + release.sha256,
                    "browser_download_url": release.asset_url,
                }]}
                for release in releases
            ]
            return io.BytesIO(json.dumps(values).encode())
        assert url in {release.asset_url for release in releases}
        return io.BytesIO(data)

    Path("canary.toml").write_text(
        f"port = {settings.port}\nmin_free_gb = 0\nallow_full_copy = true\n"
        "deadline_s = 2\nstall_s = 0.6\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(managed, "fetch_url", fetch)
    monkeypatch.setattr(managed, "OllamaRuntime",
                        partial(OllamaRuntime, argv_factory=argv_factory, ready_s=4, stop_s=0.5))
    return calls


def read_reports(home):
    reports = {}
    for path in (home / "results").glob("*_ollama-*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        reports[data["runtime"]["version"]] = data
    return reports


def test_dry_run_is_read_only(home, settings, monkeypatch, capsys):
    calls = managed_fixture(monkeypatch, settings)
    assert main(["run", "--ollama", "last:1", "--models", "x", "--dry-run"]) == 0
    assert calls == [API_URL + "?per_page=50"]
    assert not home.exists()
    out = capsys.readouterr().out
    assert "Free disk space" in out and DEFAULT_ASSET in out and "Pull into master if missing: x" in out


def test_managed_run_end_to_end(home, settings, monkeypatch, capsys):
    monkeypatch.setenv("OLLAMA_MODELS", str(home.parent / "untouched"))
    managed_fixture(monkeypatch, settings)
    assert main(["run", "--ollama", "last:1", "--models", "x"]) == 0
    report = read_reports(home)["1.10.0"]
    assert report["schema_version"] == 2
    assert report["runtime"]["tag"] == "v1.10.0"
    assert report["runtime"]["asset"] == DEFAULT_ASSET
    assert len(report["runtime"]["sha256"]) == 64
    env = report["server_env"]
    assert Path(env["HOME"]) == home / "homes/ollama-1.10.0"
    assert Path(env["OLLAMA_MODELS"]) == home / "stores/ollama-1.10.0"
    assert "PATH" not in env
    assert not (home.parent / "untouched").exists()
    assert "pulled x" in report["master_log"]["tail"]
    assert any("Metal offloaded" in line for line in report["log"]["matches"])
    steps = [
        "server start Ollama 1.10.0 master", "pull x", "create canary-x", "server stop Ollama 1.10.0 master",
        "clone_store Ollama 1.10.0", "server start Ollama 1.10.0",
        "warm-up x", "checks x", "server stop Ollama 1.10.0",
    ]
    trace = report["disk_trace"]
    assert [sample["step"] for sample in trace] == [
        f"{phase} {step}" for step in steps for phase in ("before", "after")
    ]
    assert all(type(sample["free_bytes"]) is int and "swap_used_bytes" in sample for sample in trace)
    assert [line.split(": free_bytes", 1)[0] for line in capsys.readouterr().out.splitlines()
            if line.startswith("Disk ")] == [f"Disk {step}" for step in steps]
    model, = report["models"]
    assert model["digest"] != model["derived_digest"]
    assert model["derived_model"] == "canary-x"
    assert model["thinking"] and model["context_length"] == 131072
    assert "thinking" in model["capabilities"]
    assert model["gpu"] == {
        "size": 1024, "size_vram": 768, "offload": 0.75,
        "runner": "fake-runner", "digest": model["derived_digest"],
    }
    assert [check["check_id"] for check in model["checks"]] == ["A1", "A2", "A3", "A4", "A5"]
    for check in model["checks"]:
        assert check["outcome"] == "pass"
        assert len(check["attempts"]) == 3
        for attempt in check["attempts"]:
            body = json.loads(attempt["request"]["body"])
            assert body["model"] == "canary-x"
            assert body["reasoning_effort"] == "none"

    assert main(["run", "--ollama", "last:1", "--models", "x", "--checks", "A1"]) == 0
    assert len(list((home / "results").glob("*_ollama-*.json"))) == 2
    assert sum("pulled x" in path.read_text(encoding="utf-8") for path in home.glob("logs/*.log")) == 1


def test_cpu_fallback_records_failure_and_other_version_continues(home, settings, monkeypatch):
    def argv(binary):
        mode = "cpu_fallback" if binary.parent.name == "1.10.0" else "ok"
        return fake_argv(binary, mode=mode)

    managed_fixture(monkeypatch, settings, versions=("1.9.0", "1.10.0"), argv_factory=argv)
    assert main(["run", "--ollama", "last:2", "--models", "x", "--checks", "A1"]) == 2
    reports = read_reports(home)
    failed = reports["1.10.0"]
    assert "infra_error" not in failed
    assert failed["models"][0]["model_error"]["code"] == "cpu_fallback"
    assert failed["models"][0]["gpu"]["offload"] == 0
    assert failed["models"][0]["checks"] == [{"check_id": "A1", "outcome": "error", "attempts": []}]
    assert reports["1.9.0"]["models"][0]["checks"][0]["outcome"] == "pass"


def test_decision_only_model_is_skipped_before_warmup(home, settings, monkeypatch):
    managed_fixture(monkeypatch, settings)
    args = ["run", "--ollama", "last:1", "--models", "decision-only,x", "--checks", "A1,A3", "--repeats", "1"]
    assert main(args) == 0
    report = read_reports(home)["1.10.0"]
    decision, supported = report["models"]
    assert decision["capabilities"] == ["decision"]
    assert decision["gpu"] is None and decision["derived_digest"] is None
    assert decision["outcome"] == "n/a"
    assert decision["checks"] == [
        {"check_id": check, "outcome": "n/a", "reason": "model_lacks_tools", "attempts": []}
        for check in ("A1", "A3")
    ]
    assert all(check["outcome"] == "pass" for check in supported["checks"])
    assert not any("decision-only" in step["step"] for step in report["disk_trace"]
                   if any(word in step["step"] for word in ("create ", "warm-up ", "checks ")))


def test_warmup_http_error_preserves_response_and_continues_same_version(home, settings, monkeypatch):
    managed_fixture(monkeypatch, settings)
    args = ["run", "--ollama", "last:1", "--models", "warmup-error,x", "--checks", "A1,A3", "--repeats", "1"]
    assert main(args) == 2
    report = read_reports(home)["1.10.0"]
    assert "infra_error" not in report
    failed, supported = report["models"]
    assert failed["model_error"] == {
        "code": "api_error", "message": "/v1/chat/completions: http_error",
        "http_status": 400, "body_excerpt": json.dumps({"error": "model cannot complete: " + "x" * 3000})[:2048],
    }
    assert failed["checks"] == [
        {"check_id": check, "outcome": "error", "attempts": []} for check in ("A1", "A3")
    ]
    assert all(check["outcome"] == "pass" for check in supported["checks"])
    assert any(sample["step"] == "after warm-up warmup-error" for sample in report["disk_trace"])


@pytest.mark.parametrize("inject_stop_error", [False, True])
def test_dead_server_ends_version(home, settings, monkeypatch, inject_stop_error):
    managed_fixture(monkeypatch, settings)
    if inject_stop_error:
        original_stop = runtime_ollama.OllamaServer._stop

        def stop(server):
            original_stop(server)
            if server.process.returncode == 7:
                raise PermissionError(errno.EPERM, "fixture stop failure")

        monkeypatch.setattr(runtime_ollama.OllamaServer, "_stop", stop)
    args = ["run", "--ollama", "last:1", "--models", "server-dead,x", "--checks", "A1", "--repeats", "1"]
    assert main(args) == 2
    report = read_reports(home)["1.10.0"]
    assert report["infra_error"]["code"] == "server_unreachable"
    assert report["models"][0]["model_error"]["message"] == "/v1/chat/completions: connection_error"
    if inject_stop_error:
        assert "fixture stop failure" in report["log"]["stop_note"]
        assert "fixture stop failure" not in report["infra_error"]["message"]
    assert report["models"][0]["checks"] == [{"check_id": "A1", "outcome": "error", "attempts": []}]
    assert report["models"][1]["checks"] == []
    assert not any(sample["step"] == "before warm-up x" for sample in report["disk_trace"])
    assert report["disk_trace"][-1]["step"] == "after server stop Ollama 1.10.0"


@pytest.mark.parametrize(("platform", "usage", "expected"), [
    ("darwin", "vm.swapusage: total = 2048.00M  used = 125.50M  free = 1922.50M", 131596288),
    ("darwin", "vm.swapusage: total = 3.00G used = 1.50G free = 1.50G", 1610612736),
    ("darwin", "unexpected output", None),
    ("win32", None, None),
])
def test_swap_used_bytes(platform, usage, expected, monkeypatch):
    monkeypatch.setattr(runtime_ollama, "sys", SimpleNamespace(platform=platform))

    def read(argv, **kwargs):
        assert platform == "darwin"
        assert argv == ["/usr/sbin/sysctl", "-n", "vm.swapusage"]
        return usage

    monkeypatch.setattr(runtime_ollama.subprocess, "check_output", read)
    assert runtime_ollama._swap_used_bytes() == expected


def test_requires_newer_version_yields_na(home, settings, monkeypatch):
    managed_fixture(
        monkeypatch, settings, versions=("1.9.0", "1.10.0"),
        argv_factory=partial(fake_argv, requires="1.10.0"),
    )
    assert main(["run", "--ollama", "last:2", "--models", "x", "--checks", "A1"]) == 0
    report = read_reports(home)["1.9.0"]
    model, = report["models"]
    assert model["outcome"] == "n/a"
    assert model["checks"] == [{"check_id": "A1", "outcome": "n/a", "attempts": []}]
    assert model["gpu"] is None
    assert not report["log"]["matches"]


def test_pull_error_is_infrastructure_failure(home, settings, monkeypatch):
    managed_fixture(monkeypatch, settings, argv_factory=partial(fake_argv, mode="pull_error"))
    assert main(["run", "--ollama", "last:1", "--models", "x"]) == 2
    assert read_reports(home)["1.10.0"]["infra_error"]["code"] == "pull_failed"


def test_managed_output_refuses_paths_outside_home(home, settings, monkeypatch):
    calls = managed_fixture(monkeypatch, settings)
    args = ["run", "--ollama", "last:1", "--models", "x", "--results-dir", "../outside"]
    assert main(args) == 2
    assert not calls and not home.exists() and not (home.parent / "outside").exists()
