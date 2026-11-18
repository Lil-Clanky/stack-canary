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
from stack_canary.repro import DRAFT_MARKER
from test_runtime_ollama import home

TAG = "v0.23.2"
SHA = "c" * 64
BODY = '{"model":"canary-a","stream":true,"messages":[{"role":"user","content":"A test\'s prompt"}]}'


def _check(check, outcome, *, attempts=(), **fields):
    return {"check_id": check, "outcome": outcome, "attempts": list(attempts), **fields}


def _record(directory, stamp, version, checks, *, model_name="a", **fields):
    directory.mkdir(parents=True, exist_ok=True)
    model = {"name": model_name, "digest": "sha256:" + SHA, "derived_model": "canary-" + model_name,
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


def test_report_prunes_only_its_stale_drafts(home):
    directory = Path("repro")
    directory.mkdir()
    original = directory / "2026-11-15_ollama-0.40.16_a_A1.md"
    original.write_text("# My notes\n", encoding="utf-8")
    unchanged = directory / "old.md"
    unchanged.write_text("# DRAFT — not submitted\n" + DRAFT_MARKER + "\n", encoding="utf-8")
    _record(home / "results", "20261115T120000Z", "0.40.16", [_check("A1", "fail")])
    assert main(["report"]) == 0
    generated = directory / "2026-11-15_ollama-0.40.16_a_A1_2.md"
    assert generated.read_text(encoding="utf-8").splitlines()[0] == DRAFT_MARKER
    assert original.read_text(encoding="utf-8") == "# My notes\n"
    assert main(["report"]) == 0
    assert generated.exists()
    _record(home / "results", "20261116T120000Z", "0.40.16", [_check("A1", "pass")])
    assert main(["report"]) == 0
    assert set(directory.iterdir()) == {original, unchanged}


def test_report_defaults_to_the_configured_models(home):
    directory = home / "results"
    _record(directory, "20261115T120000Z", "0.40.15", [_check("A1", "fail")], model_name="tev1:4b")
    _record(directory, "20261116T120000Z", "0.40.16", [_check("A1", "pass")], model_name="llama3.2:3b")
    (directory / "20261117T120000Z_endpoint.json").write_text(json.dumps({
        "schema_version": 1, "runtime_version": "0.40.16",
        "models": [{"name": "endpoint-only", "checks": [_check("A1", "pass")]}],
    }), encoding="utf-8")
    assert main(["report", "--all-models"]) == 0
    page = Path("report/index.html")
    assert "tev1:4b" in page.read_text(encoding="utf-8")
    assert len(list(Path("repro").glob("*.md"))) == 1
    assert main(["report"]) == 0
    html = page.read_text(encoding="utf-8")
    assert "llama3.2:3b" in html and "tev1:4b" not in html and "endpoint-only" not in html
    assert not list(Path("repro").glob("*.md"))
    # A watch rerun of one configured model keeps the other configured models visible.
    _record(directory, "20261118T120000Z", "0.40.16", [_check("A1", "pass")], model_name="qwen3.5:0.8b")
    assert main(["report"]) == 0
    html = page.read_text(encoding="utf-8")
    assert "llama3.2:3b" in html and "qwen3.5:0.8b" in html and "tev1:4b" not in html


def test_historical_cli_rejection_is_not_an_upstream_failure(home):
    before = {"outcome": "fail", "reason": "file_missing", "attribution": "harness",
              "exit_code": 1, "stderr_tail": ["Error: Unsupported gateway CLI options: --run-budget."],
              "proxy_exchanges": []}
    _record(home / "results", "20261115T120000Z", "0.40.16", [_check("B6", "fail", attempts=[before])])
    cells, _, _ = merge(load_runs([home / "results"]))
    check = cells["0.40.16", "a", "B6", TAG].record
    assert (check["outcome"], check["reason"]) == ("error", "harness_cli_rejected")
    assert check["attempts"][0]["attribution"] == "canary"
    assert main(["report"]) == 0
    assert not list(Path("repro").glob("*.md"))


def test_b6_report_distinguishes_runtime_harness_and_startup(home):
    directory = home / "results"
    for index, name in enumerate(("runtime", "harness", "draining")):
        _record(directory, f"20261115T12000{index}Z", "0.40.15",
                [_check("B6", "pass")], model_name=name)

    def exchange(path, method, status):
        return {"request": {"path": path, "method": method, "body": BODY},
                "response": {"status": status}, "error": "http_error"}

    scenarios = {
        "runtime": [exchange("/v1/chat/completions", "POST", 500)],
        "harness": [exchange("/api/v1/models", "GET", 404)],
        "draining": [],
    }
    for index, (name, exchanges) in enumerate(scenarios.items()):
        attempt = {"outcome": "fail", "reason": "file_missing", "attribution": "runtime",
                   "exit_code": 1, "stderr_tail": ["Error: Gateway draining: not_ready"] if not exchanges else [],
                   "proxy_exchanges": exchanges}
        _record(directory, f"20261115T13000{index}Z", "0.40.16",
                [_check("B6", "fail", attempts=[attempt])], model_name=name)
    repro_dir = Path("repro")
    repro_dir.mkdir()
    stale = repro_dir / "stale-draining.md"
    stale.write_text(DRAFT_MARKER + "\n", encoding="utf-8")
    assert main(["report", "--all-models"]) == 0
    html = Path("report/index.html").read_text(encoding="utf-8")
    assert html.count('class="regression">Regression') == 1
    assert html.count('class="harness-failure">Harness failure') == 2
    assert "harness_not_started" in html
    assert not stale.exists()
    assert sorted(path.stem for path in repro_dir.glob("*.md")) == [
        "2026-11-15_ollama-0.40.16_harness_B6", "2026-11-15_ollama-0.40.16_runtime_B6",
    ]


def test_error_after_pass_is_not_a_regression(home):
    _record(home / "results", "20261114T100000Z", "0.40.15", [_check("A1", "pass"), _check("A2", "pass")])
    _record(home / "results", "20261115T100000Z", "0.40.16", [_check("A1", "error"), _check("A2", "fail")])
    assert main(["report"]) == 0
    row = next(line for line in Path("report/MATRIX.md").read_text(encoding="utf-8").splitlines()
               if line.startswith("| 0.40.16"))
    assert row.split("|")[2:4] == [" ⚠️ ", " ❌ 🔻 "]
