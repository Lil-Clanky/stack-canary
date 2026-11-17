import hashlib
import json
from types import SimpleNamespace

from stack_canary import isolation, managed
from stack_canary.cli import main
from test_runtime_ollama import home, managed_fixture, port, read_reports, settings


def test_snapshot_and_diff_only_read_fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(isolation, "_api", lambda path: {"version": "0.40.14"} if path.endswith("version") else None)
    monkeypatch.setattr(isolation, "_services", lambda: (None, None))
    assert isolation.snapshot(tmp_path)["hermes_env_sha256"] is None
    assert isolation.snapshot(tmp_path)["manifests_sha256"] is None
    files = {
        ".ollama/models/manifests/host/library/model/latest": b"old manifest",
        ".ollama/models/manifests-v2/host/library/model/latest": b"v2 manifest",
        ".ollama/models/blobs/sha256-blob": b"old blob",
        ".hermes/config.yaml": b"config: sample",
        ".hermes/.env": b"SECRET_VALUE_FOR_TEST",
    }
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    first = isolation.snapshot(tmp_path)
    assert first["hermes_env_sha256"] == hashlib.sha256(files[".hermes/.env"]).hexdigest()
    assert first["manifests_sha256"] and first["manifests_v2_sha256"]
    assert first["blobs"]["sha256-blob"]["size"] == len(b"old blob")
    assert isolation.compare(first, isolation.snapshot(tmp_path))["status"] == "pass"
    assert all((tmp_path / name).read_bytes() == content for name, content in files.items())
    (tmp_path / ".hermes/.env").write_bytes(b"UPDATED_SECRET")
    (tmp_path / ".ollama/models/blobs/sha256-blob").write_bytes(b"larger blob")
    (tmp_path / ".ollama/models/manifests/host/library/model/latest").write_bytes(b"new")
    audit = isolation.compare(first, isolation.snapshot(tmp_path))
    assert audit["status"] == "isolation_violated"
    assert set(audit["diff"]) == {"hermes_env_sha256", "manifests_sha256", "blobs"}
    assert audit["diff"]["blobs"]["sha256-blob"]["after"]["size"] == len(b"larger blob")
    assert "SECRET" not in json.dumps(audit)


def test_launchd_and_listener_changes_are_warnings():
    before = {"launchd": ["12 0 ollama"], "listeners": {"13": ["n127.0.0.1:9119"]}}
    after = {"launchd": ["14 0 ollama"], "listeners": {"15": ["n127.0.0.1:9119"]}}
    audit = isolation.compare(before, after)
    assert audit["status"] == "warning"
    assert audit["diff"] == {}
    assert set(audit["warnings"]) == {"launchd", "listeners"}


def test_mac_metadata_probes_have_a_read_only_scope(monkeypatch):
    monkeypatch.setattr(isolation, "sys", SimpleNamespace(platform="darwin"))

    def command(argv, **kwargs):
        if argv == ["/bin/launchctl", "list"]:
            return "37 0 ai.hermes.gateway\n38 0 unrelated.service\n36 0 com.ollama"
        assert argv == ["/usr/sbin/lsof", "-nP", "-iTCP:9119", "-iTCP:9120", "-sTCP:LISTEN", "-Fpcn"]
        return "p37\nchermes\nn127.0.0.1:9119\n"

    monkeypatch.setattr(isolation, "_command", command)
    assert isolation._services() == (["36 0 com.ollama", "37 0 ai.hermes.gateway"],
                                      {"37": ["chermes", "n127.0.0.1:9119"]})


def test_managed_isolation_violation_sets_exit_code_and_retains_matrix(home, settings, monkeypatch):
    managed_fixture(monkeypatch, settings)
    snapshots = iter([
        {"ollama_version": {"version": "0.40.14"}},
        {"ollama_version": {"version": "0.40.13"}},
    ])
    monkeypatch.setattr(managed.isolation, "snapshot", lambda: next(snapshots))
    args = ["run", "--ollama", "last:1", "--models", "x", "--checks", "A1", "--repeats", "1"]
    assert main(args) == 2
    result = read_reports(home)["1.10.0"]
    assert result["models"][0]["checks"][0]["outcome"] == "pass"
    assert result["isolation"]["status"] == "isolation_violated"
    assert result["isolation"]["diff"]["ollama_version"]["after"] == {"version": "0.40.13"}
