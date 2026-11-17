import io
import json
import os
import time
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from stack_canary import releases
from stack_canary.harness_hermes import API as HERMES_API
from stack_canary.harness_hermes import hermes_tags
from stack_canary.releases import API_URL, DEFAULT_ASSET, ollama_releases

SHA = "0a" * 32
SUM_URL = "https://example.invalid/sha256sum.txt"


def release_record(version, *, digest=f"sha256:{SHA}", asset=True, **kwargs):
    return {
        "tag_name": f"v{version}",
        "assets": ([
            {"name": DEFAULT_ASSET, "digest": digest,
             "browser_download_url": f"https://example.invalid/{version}/{DEFAULT_ASSET}"},
            {"name": "sha256sum.txt", "browser_download_url": SUM_URL},
        ] if asset else []),
        **kwargs,
    }


def test_stable_release_sort_and_checksums(monkeypatch, capsys):
    calls = []

    def fetch(url, *, headers):
        calls.append((url, headers))
        if url == SUM_URL:
            return io.BytesIO(f"{'B' * 64}  *./{DEFAULT_ASSET}\n".encode())
        return io.BytesIO(json.dumps([
            release_record("0.9.1", published_at="2026-12-01"),
            release_record("0.12.0-rc1", prerelease=True),
            release_record("0.10.0", digest=None, published_at="2026-01-01"),
            release_record("1.2.0", asset=False),
            release_record("2.0.0", draft=True),
            release_record("3.0.0", prerelease=True),
            release_record("not-semver"),
        ]).encode())

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    result = ollama_releases("last:2", fetch=fetch)
    assert [release.version for release in result] == ["0.10.0", "0.9.1"]
    assert [release.sha256 for release in result] == ["b" * 64, SHA]
    assert result[0].asset_name == DEFAULT_ASSET
    assert calls == [
        (API_URL + "?per_page=50", {"User-Agent": "stack-canary", "Authorization": "Bearer test-token"}),
        (SUM_URL, {"User-Agent": "stack-canary"}),
    ]
    assert "skipping v1.2.0" in capsys.readouterr().err


def test_explicit_tags_and_custom_asset(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls = []
    name = "custom.tgz"

    def fetch(url, *, headers):
        calls.append(url)
        assert headers == {"User-Agent": "stack-canary"}
        record = release_record(url.rsplit("/v", 1)[-1])
        record["assets"][0]["name"] = name
        return io.BytesIO(json.dumps(record).encode())

    releases = ollama_releases("0.9.4, v0.10.0", asset_name=name, fetch=fetch)
    assert calls == [API_URL + "/tags/v0.9.4", API_URL + "/tags/v0.10.0"]
    assert [release.version for release in releases] == ["0.10.0", "0.9.4"]
    assert all(release.asset_name == name for release in releases)


def test_unknown_tag():
    def fetch(url, *, headers):
        raise HTTPError(url, 404, "not found", {}, None)

    with pytest.raises(ValueError, match="unknown Ollama release"):
        ollama_releases("0.0.0", fetch=fetch)


@pytest.mark.parametrize("spec", ["last:0", "last:-1", "../5.0", "1.2.3-rc1"])
def test_bad_release_spec(spec):
    with pytest.raises(ValueError):
        ollama_releases(spec, fetch=lambda *args, **kwargs: pytest.fail("fetched"))


def test_cache_keeps_both_listings_for_an_hour(tmp_path):
    calls = []

    def fetch(url, *, headers):
        calls.append(url)
        rows = [release_record("1.2.3")] if url.startswith(API_URL) else [{"tag_name": "v0.23.2"}]
        return io.BytesIO(json.dumps(rows).encode())

    opts = {"fetch": fetch, "cache_root": tmp_path}
    for _ in range(2):
        assert hermes_tags("latest", **opts) == ["v0.23.2"]
        assert ollama_releases("last:1", **opts)[0].version == "1.2.3"
    assert calls == [HERMES_API + "?per_page=50", API_URL + "?per_page=50"]

    cache = tmp_path / "release-cache/hermes.json"
    expired = time.time() - 3601
    os.utime(cache, (expired, expired))
    assert hermes_tags("latest", **opts, read_only=True) == ["v0.23.2"]
    assert cache.stat().st_mtime == pytest.approx(expired, abs=1)
    assert hermes_tags("latest", **opts) == ["v0.23.2"]
    assert hermes_tags("latest", **opts) == ["v0.23.2"]
    assert calls[2:] == [HERMES_API + "?per_page=50"] * 2


def test_gh_token_is_private_and_only_used_on_github_api(monkeypatch, capsys):
    requests = []
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        assert kwargs["stderr"] == releases.subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout="gh-test-secret\n")

    def open_url(request, *, timeout):
        requests.append(request)
        return io.BytesIO(b"[]")

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(releases.subprocess, "run", command)
    monkeypatch.setattr(releases, "build_opener", lambda *args: SimpleNamespace(open=open_url))
    assert releases._api_json(API_URL, releases.fetch_url) == []
    releases.fetch_url(SUM_URL, headers={"User-Agent": "stack-canary"}).close()
    monkeypatch.setenv("GITHUB_TOKEN", "env-test-secret")
    assert releases._api_json(API_URL, releases.fetch_url) == []
    assert calls == [["gh", "auth", "token", "--hostname", "github.com"]]
    assert [req.unredirected_hdrs.get("Authorization") for req in requests] == [
        "Bearer gh-test-secret", None, "Bearer env-test-secret",
    ]
    assert all("Authorization" not in req.headers for req in requests)
    assert capsys.readouterr() == ("", "")
