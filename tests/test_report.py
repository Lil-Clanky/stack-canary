import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from fake_ollama import running
from stack_canary.checks.api import run_attempt
from stack_canary.cli import main
from stack_canary.report import load_runs, merge
from test_runtime_ollama import home

TAG = "v0.23.2"
SHA = "c" * 64
BODY = '{"model":"canary-a","stream":true,"messages":[{"role":"user","content":"A test\'s prompt"}]}'


def _check(check, outcome, *, attempts=(), **fields):
    return {"check_id": check, "outcome": outcome, "attempts": list(attempts), **fields}


def _record(directory, stamp, version, checks, **fields):
    directory.mkdir(parents=True, exist_ok=True)
    model = {"name": "a", "digest": "sha256:" + SHA, "derived_model": "canary-a",
             "source": "present", "gpu": {"runner": "llamacpp", "offload": 1.0},
             "checks": checks}
    result = {
        "schema_version": 2,
        "runtime": {"version": version, "sha256": SHA},
        "canary_version": "0.1.0.dev0",
        "host": {"chip": "Apple M4", "ram_bytes": 16 * 1024**3, "macos": "26.6"},
        "server_env": {"OLLAMA_HOST": "127.0.0.1:11535", "OLLAMA_CONTEXT_LENGTH": "65536"},
        "settings": {"checks": ["A1", "A2", "A3", "A4", "A5", "B6"]},
        "harness": {"tag": TAG, "commit": "ae2a81c"},
        "isolation": {"status": "pass"},
        "models": [model],
        **fields,
    }
    path = directory / f"{stamp}_ollama-{version}.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    return path


def test_matrix_and_repro_drafts_keep_latest_real_outcomes(home):
    repo_results = Path("results")
    canary_results = home / "results"
    _record(repo_results, "20261114T100000Z", "0.40.15", [_check("A2", "pass")])
    _record(repo_results, "20261114T110000Z", "0.40.16", [_check("A1", "fail")])
    failed = {"outcome": "fail", "reason": "no_done",
              "request": {"method": "POST", "url": "http://localhost:11535/v1/chat/completions", "body": BODY},
              "response": {"status": 200, "lines": [{"line": "data: {}"}]}}
    proxied = {"outcome": "fail", "reason": "file_missing", "argv": ["hermes", "--api-key", "secret-token"],
               "proxy_exchanges": [{"error": "http_error",
                                    "request": {"method": "POST", "path": "/v1/chat/completions", "body": BODY},
                                    "response": {"status": 500, "body": "bad gateway"}}]}
    _record(canary_results, "20261115T120000.123456Z", "0.40.16", [
        _check("A1", "pass", attempts=({"outcome": "pass"},) * 3),
        _check("A2", "fail", attempts=(failed,) * 3),
        _check("A3", "flaky", attempts=({"outcome": "pass"}, failed, failed)),
        _check("A4", "timeout", attempts=({"outcome": "timeout", "reason": "timeout_stall",
                                         "response": {"status": 200, "body": "\udcff"}},)),
        _check("A5", "n/a", reason="model_lacks_tools"),
        _check("B6", "fail", attempts=(proxied,)),
    ])
    _record(repo_results, "20261116T000000Z", "0.40.16", [],
            harness={"releases": []}, hermes_tags=[TAG, "v0.23.3"], isolation={"status": "warning"},
            infra_error={"code": "hermes_setup_failed", "message": "Cannot install <fixture>"})
    cells, _, _ = merge(load_runs([repo_results, canary_results]))
    assert cells["0.40.16", "a", "A1", ""].outcome == "pass"
    assert cells["0.40.16", "a", "B6", TAG].outcome == "fail"
    assert cells["0.40.16", "a", "B6", "v0.23.3"].outcome == "error"

    assert main(["report"]) == 0
    html = Path("report/index.html").read_text(encoding="utf-8")
    for fragment in ("<title>Stack Canary compatibility report</title>", "prefers-color-scheme: dark",
                     'class="regression">Regression', 'title="Attempt 3: fail"',
                     "Apple M4", "16.0 GiB", "v0.23.2 (ae2a81c)", "Latest run isolation</dt><dd>warning",
                     "Runner llamacpp · GPU 100.0% · Source present", "model_lacks_tools",
                     "Cannot install &lt;fixture&gt;", "B6 v0.23.3"):
        assert fragment in html
    assert html.index('rowspan="2">0.40.16') < html.index('rowspan="2">0.40.15')
    for outcome in ("pass", "fail", "flaky", "timeout", "error", "n/a"):
        assert f">{outcome}</li>" in html
    paths = sorted(Path("repro").glob("*.md"))
    assert len(paths) == 4
    a2 = Path("repro/2026-11-15_ollama-0.40.16_a_A2.md").read_text(encoding="utf-8")
    assert "DRAFT — not submitted" in a2 and "no_done" in a2
    assert "Last older Ollama version that passed this cell: 0.40.15" in a2
    assert "ollama pull a" in a2 and "/api/create" in a2 and " -N " in a2
    assert "../repro/2026-11-15_ollama-0.40.16_a_A2.md" in html
    b6 = next(path for path in paths if "_B6" in path.name).read_text(encoding="utf-8")
    assert TAG in b6 and "ae2a81c" in b6 and "[redacted]" in b6
    assert "secret-token" not in b6
    assert "bad gateway" in b6 and "--data-binary" in b6
    a4 = next(path for path in paths if "_A4" in path.name).read_text(encoding="utf-8")
    assert r"\udcff" in a4


@pytest.mark.skipif(shutil.which("curl") is None, reason="requires curl to replay a generated command")
def test_repro_curl_replays_missing_done_against_fake_server(home):
    with running("no_done") as fake:
        attempt = run_attempt("A2", fake.endpoint, "a", 1, deadline_s=2, stall_s=0.6)
        assert attempt["reason"] == "no_done"
        _record(home / "results", "20261115T120000Z", "0.40.16", [_check("A2", "fail", attempts=[attempt])])
        assert main(["report", "--results", str(home / "results"), "--out", "report/custom.html",
                     "--repro-dir", "repro/custom"]) == 0
        draft, = Path("repro/custom").glob("*.md")
        lines = draft.read_text(encoding="utf-8").splitlines()
        argv = shlex.split(next(line for line in lines if line.startswith("curl ") and " -N " in line))
        assert argv[argv.index("--data-binary") + 1] == attempt["request"]["body"]
        argv[-1] = fake.endpoint + "/v1/chat/completions"
        response = subprocess.run(argv, check=True, capture_output=True, timeout=5).stdout
        assert b"data:" in response and b"[DONE]" not in response


def test_report_refuses_output_outside_allowed_directories(home, capsys):
    _record(home / "results", "20261115T120000Z", "0.40.16", [_check("A1", "pass")])
    assert main(["report", "--out", "outside.html"]) == 2
    assert "output must stay" in capsys.readouterr().err
    assert not Path("outside.html").exists()
