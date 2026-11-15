"""Resolve stable Ollama releases and their verified asset metadata."""

import json
import os
import re
import sys
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_URL = "https://api.github.com/repos/ollama/ollama/releases"
DEFAULT_ASSET = "ollama-darwin.tgz"
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SHA256 = re.compile(r"[0-9a-fA-F]{64}")


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    asset_name: str
    asset_url: str
    sha256: str


def version_key(version: str) -> tuple[int, ...]:
    if not VERSION.fullmatch(version):
        raise ValueError(f"invalid Ollama version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def asset_filename(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise ValueError("asset_name must be a filename")
    return name


def _https_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port == 11434
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("release metadata and assets require an HTTPS URL without credentials")


class _HttpsRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url: str, *, headers: dict[str, str]):
    _https_url(url)
    request = Request(url)
    for name, value in headers.items():
        if name == "Authorization":
            request.add_unredirected_header(name, value)
        else:
            request.add_header(name, value)
    return build_opener(_HttpsRedirect()).open(request, timeout=60)


def _api_json(url, fetch):
    headers = {"User-Agent": "stack-canary"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    with fetch(url, headers=headers) as stream:
        return json.load(stream)


def _stable_version(record: object) -> str | None:
    if not isinstance(record, dict) or record.get("draft") or record.get("prerelease"):
        return None
    tag = record.get("tag_name")
    if isinstance(tag, str) and tag.startswith("v") and VERSION.fullmatch(tag[1:]):
        return tag[1:]
    return None


def _asset(record: dict, name: str, fetch) -> Release | None:
    entries = record.get("assets")
    assets = {
        asset["name"]: asset
        for asset in (entries if isinstance(entries, list) else [])
        if isinstance(asset, dict) and isinstance(asset.get("name"), str)
    }
    asset = assets.get(name, {})
    url = asset.get("browser_download_url")
    tag = record["tag_name"]
    if not isinstance(url, str) or not url:
        print(f"canary: skipping {tag}, missing {name}", file=sys.stderr)
        return None
    digest = asset.get("digest")
    sha = digest[7:] if isinstance(digest, str) and digest.startswith("sha256:") else ""
    if not SHA256.fullmatch(sha):
        sha = ""
        checksum_url = assets.get("sha256sum.txt", {}).get("browser_download_url")
        if isinstance(checksum_url, str) and checksum_url:
            with fetch(checksum_url, headers={"User-Agent": "stack-canary"}) as stream:
                for line in stream.read().decode("utf-8").splitlines():
                    parts = line.split(maxsplit=1)
                    if len(parts) == 2 and parts[1].lstrip("*").removeprefix("./") == name:
                        sha = parts[0]
                        break
    if not SHA256.fullmatch(sha):
        raise ValueError(f"{tag}: no SHA-256 for {name}")
    return Release(tag[1:], tag, name, url, sha.lower())


def ollama_releases(spec: str, *, asset_name: str = DEFAULT_ASSET, fetch=fetch_url) -> list[Release]:
    """Resolve newest-first releases with the requested asset."""
    asset_filename(asset_name)
    if spec.startswith("last:"):
        count = spec.removeprefix("last:")
        if not re.fullmatch(r"[0-9]+", count) or int(count) < 1:
            raise ValueError("use last:N with a positive release count")
        limit = int(count)
        listing = _api_json(API_URL + "?per_page=50", fetch)
        if not isinstance(listing, list):
            raise ValueError("expected a GitHub release list")
        records = {
            version: record
            for record in listing
            if (version := _stable_version(record)) is not None
        }
    else:
        versions = list(dict.fromkeys(part.strip().removeprefix("v") for part in spec.split(",")))
        for version in versions:
            version_key(version)
        limit = len(versions)
        records = {}
        for version in versions:
            try:
                record = _api_json(API_URL + f"/tags/v{version}", fetch)
            except HTTPError as error:
                if error.code == 404:
                    raise ValueError(f"unknown Ollama release v{version}") from error
                raise
            if _stable_version(record) != version:
                raise ValueError(f"unknown stable Ollama release v{version}")
            records[version] = record

    releases = []
    for version in sorted(records, key=version_key, reverse=True):
        release = _asset(records[version], asset_name, fetch)
        if release is not None:
            releases.append(release)
        if len(releases) == limit:
            break
    if not releases:
        raise ValueError(f"no stable Ollama releases with {asset_name}")
    return releases
