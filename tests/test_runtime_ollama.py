import hashlib
import io
import json
import os
import socket
import sys
import tarfile
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
    assert (home / "downloads/1.10.0" / DEFAULT_ASSET).read_bytes() == data
    assert runtime.install(release) == executable
    assert calls == [release.asset_url]


@pytest.mark.parametrize("extra", [
    {"../outside": b"rejected"},
    {"/absolute": b"rejected"},
    {"C:/absolute": b"rejected"},
    {"link": (tarfile.SYMTYPE, "../../outside")},
    {"link": (tarfile.LNKTYPE, "../../outside")},
    {"fifo": (tarfile.FIFOTYPE, "")},
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


def test_managed_run_end_to_end(home, settings, monkeypatch):
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
    assert failed["infra_error"]["code"] == "cpu_fallback"
    assert failed["models"][0]["gpu"]["offload"] == 0
    assert failed["models"][0]["checks"] == []
    assert reports["1.9.0"]["models"][0]["checks"][0]["outcome"] == "pass"


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
