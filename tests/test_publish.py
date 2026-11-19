import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from stack_canary import publish
from stack_canary.config import Settings
from stack_canary.harness_hermes import API_KEY


@pytest.mark.parametrize("secret", ["fixture-topic", API_KEY, "ghp_abcde_fake", "fixture-auth",
                                  "Authorization: Bearer 0123456789abcdef"])
def test_publish_refuses_secrets(secret):
    with pytest.raises(ValueError, match="credential-like"):
        publish._sanitize(secret, Settings(ntfy_topic="fixture-topic"), "fixture-auth")


def test_orphan_publish_only_sanitized_reports(tmp_path, monkeypatch):
    repo, root = tmp_path / "repo", tmp_path / "canary"
    repo.mkdir()
    root.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CANARY_HOME", str(root))
    def git(cwd, *argv):
        return subprocess.check_output(["git", *argv], cwd=cwd, text=True).strip()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Fixture Canary")
    git(repo, "remote", "add", "origin", publish.REMOTE)
    (repo / "source.txt").write_text("never publish this\n")
    git(repo, "add", "source.txt")
    git(repo, "commit", "-m", "Source")
    main = git(repo, "rev-parse", "HEAD")
    (repo / "report").mkdir()
    (repo / "report/summary.md").write_text("# Pass\n/Users/person/private\n")
    (repo / "report/summary.json").write_text(json.dumps({"status": "pass"}))
    (root / "results").mkdir()
    (root / "results/20261117T200000Z.json").write_text(json.dumps({
        "runtime": {"version": "0.40.16"}, "isolation": {"status": "pass"},
        "models": [{"name": "fixture", "checks": [{"check_id": "A1", "outcome": "pass"}]}],
    }))
    monkeypatch.setattr(publish, "_token", lambda user: "fixture-auth")
    original = publish._git
    pushes = []

    def offline(directory, *argv, **kwargs):
        if "push" in argv:
            pushes.append((argv, kwargs["env"]["GH_TOKEN"]))
            if len(pushes) == 1:  # The network drops during the first push.
                raise ValueError("publish git operation failed (exit 128)")
            return SimpleNamespace(returncode=0)
        return original(directory, *argv, **kwargs)

    monkeypatch.setattr(publish, "_git", offline)
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: None)
    runtime = SimpleNamespace(root=root, settings=Settings(publish_branch="matrix", publish_gh_user="Lil-Clanky"))
    assert publish.publish(runtime) == publish.URL
    worktree = root / "publish"
    assert git(worktree, "rev-list", "--count", "HEAD") == "1"
    assert git(repo, "rev-parse", "HEAD") == main
    assert set(git(worktree, "ls-tree", "--name-only", "HEAD").splitlines()) == publish.ARTIFACTS
    assert "~/private" in (worktree / "summary.md").read_text()
    assert "✅" in (worktree / "MATRIX.md").read_text()
    assert all(args[-1] == "HEAD:refs/heads/matrix" and "credential.helper=!gh auth git-credential" in args
               and token == "fixture-auth" for args, token in pushes)
    publish.publish(runtime)
    assert git(worktree, "rev-list", "--count", "HEAD") == "1"
    (repo / "report/summary.md").write_text("leak " + API_KEY)
    with pytest.raises(ValueError, match="credential-like"):
        publish.publish(runtime)
    assert len(pushes) == 3
