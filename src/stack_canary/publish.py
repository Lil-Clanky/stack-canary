"""Publish sanitized compatibility artifacts to a dedicated orphan branch."""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import report
from .harness_hermes import API_KEY
from .runtime_ollama import _state_path

REMOTE = "https://github.com/Lil-Clanky/stack-canary.git"
URL = "https://github.com/Lil-Clanky/stack-canary/tree/matrix"
ARTIFACTS = {"MATRIX.md", "index.html", "summary.md", "summary.json"}
SECRET = re.compile(
    r"(?:\bgithub_pat_[A-Za-z0-9_]+|\bgh[pousr]_[A-Za-z0-9_]+|\bsk-[A-Za-z0-9_-]{12,}"
    r"|\bxox[baprs]-[A-Za-z0-9-]{10,}|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|\b(?:Bearer|Basic)\s+[A-Za-z0-9_.+/=-]{8,}"
    r"""|\b(?:token|access_token|api[_-]?key|password)["']?\s*[:=]\s*["']?[A-Za-z0-9_.+/=-]{12,})""",
    re.IGNORECASE,
)


def _git(directory, *args, checked=True, env=None):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                            cwd=directory, env=env, text=True, capture_output=True, timeout=60)
    if checked and result.returncode:
        # Git or credential helper diagnostics can include private endpoints.
        raise ValueError(f"publish git operation failed (exit {result.returncode})")
    return result


def _token(user):
    result = subprocess.run(["gh", "auth", "token", "--user", user], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=10, check=False)
    token = result.stdout.strip()
    if result.returncode or not token:
        raise ValueError("publish credential is unavailable for the configured GitHub user")
    return token


def _sanitize(text, settings, token):
    secrets = (settings.ntfy_topic, token, API_KEY, os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN"))
    if any(secret and secret in text for secret in secrets) or SECRET.search(text):
        raise ValueError("publish rejected: credential-like content in an artifact")
    return re.sub(r"/Users/[^/\s\"'<>\\]+", "~", text)


def _allowed(relative):
    return (relative.as_posix() in ARTIFACTS or
            (len(relative.parts) == 2 and relative.parts[0] == "repro"
             and re.fullmatch(r"[A-Za-z0-9._-]+\.md", relative.name)))


def _files(directory):
    found = {}
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if relative.as_posix() == ".git" and path.is_file() and not path.is_symlink():
            continue
        if relative.as_posix() == "repro" and path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or not _allowed(relative):
            raise ValueError("publish directory has an unexpected path")
        found[relative] = path
    return found


def _worktree(repo, root, branch):
    worktree = _state_path(root, Path("publish"))
    ref = f"refs/heads/{branch}"
    if not worktree.exists():
        if _git(repo, "show-ref", "--verify", "--quiet", ref, checked=False).returncode == 0:
            _git(repo, "worktree", "add", str(worktree), branch)
        else:
            _git(repo, "worktree", "add", "--detach", "--no-checkout", str(worktree), "HEAD")
            _git(worktree, "symbolic-ref", "HEAD", ref)
    if Path(_git(worktree, "rev-parse", "--show-toplevel").stdout.strip()).resolve() != worktree:
        raise ValueError("publish path is not its own git worktree")
    common = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    expected = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    if common.resolve() != expected.resolve() or _git(worktree, "symbolic-ref", "HEAD").stdout.strip() != ref:
        raise ValueError("publish worktree is not the configured matrix branch")
    head = _git(worktree, "rev-parse", "--verify", "HEAD", checked=False)
    if head.returncode == 0 and _git(worktree, "merge-base", "HEAD", "refs/heads/main", checked=False).returncode == 0:
        raise ValueError("publish branch is not orphaned from main")
    return worktree


def _render(root, repo, settings, token):
    with tempfile.TemporaryDirectory(prefix="publish-stage-", dir=root) as path:
        stage = Path(path)
        report.generate([root / settings.results_dir, root / "results", repo / "results"],
                        stage / "index.html", stage / "repro", stable_timestamp=True)
        for name in ("summary.md", "summary.json"):
            (stage / name).write_bytes((repo / "report" / name).read_bytes())
        payload = {relative: _sanitize(path.read_text(encoding="utf-8"), settings, token)
                   for relative, path in _files(stage).items()}
    if {p.as_posix() for p in payload} & ARTIFACTS != ARTIFACTS:
        raise ValueError("publish artifacts are incomplete")
    return payload


def publish(runtime):
    root, settings = runtime.root, runtime.settings
    if settings.publish_branch != "matrix" or settings.publish_gh_user is None:
        raise ValueError("configure publish_branch=matrix and publish_gh_user before publishing")
    repo = Path.cwd().resolve()
    remote = _git(repo, "remote", "get-url", "--push", "origin").stdout.splitlines()
    if remote != [REMOTE]:
        raise ValueError("publish origin is not the approved Stack Canary repository")
    token = _token(settings.publish_gh_user)
    payload = _render(root, repo, settings, token)
    worktree = _worktree(repo, root, settings.publish_branch)
    current = _files(worktree)
    for relative, path in current.items():
        if relative not in payload:
            path.unlink()
    for relative, content in payload.items():
        report._write(worktree / relative, root, content)
    _git(worktree, "add", "--force", "--all", "--", ".")
    status = _git(worktree, "diff", "--cached", "--quiet", checked=False).returncode
    if status == 1:
        _git(worktree, "commit", "-m", "Update compatibility matrix")
    elif status != 0:
        raise ValueError("could not verify the matrix index")
    # Refuse to push an unexpected path staged by concurrent external activity.
    staged = _git(worktree, "ls-files", "-z").stdout.split("\0")
    if set(p for p in staged if p) != {p.as_posix() for p in payload}:
        raise ValueError("matrix index does not match the sanitized artifact set")
    if _git(worktree, "remote", "get-url", "--push", "origin").stdout.splitlines() != [REMOTE]:
        raise ValueError("matrix worktree push URL is not approved")
    _git(worktree, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
         "push", "origin", f"HEAD:refs/heads/{settings.publish_branch}",
         env={**os.environ, "GH_TOKEN": token})
    print(f"Published matrix: {URL}")
    return URL
