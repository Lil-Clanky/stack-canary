import io
import json
from urllib.error import HTTPError

import pytest

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
