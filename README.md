# Stack Canary

Stack Canary checks protocol compatibility across releases of local LLM stacks.
The project targets Ollama and Hermes Agent so that changes such as broken tool
calls or hanging streams can be detected and reported by version, model, and
check. It measures protocol behavior.

**Status:** v0.1 in progress. Layer A runs five protocol checks in endpoint or managed Ollama mode. The full Layer A matrix passed on the Mac mini. Hermes B6 and the static compatibility report are implemented.

## Quick start

Install with Python 3.11 or newer, then run the tests.

```sh
python -m pip install -e ".[dev]"
python -m pytest
```

Start the fixture on an ephemeral loopback port.

```sh
python tests/fake_ollama.py --port 0 --mode ok
```

In a second terminal, substitute the URL printed by the fixture.

```sh
canary run --endpoint http://127.0.0.1:49152 --models a,b
```

The table shows the outcome of each check. The default is three attempts per
model and check. Use `--checks A1,A4`, `--repeats 1`, or `--results-dir DIR`
to change the run. To exercise a failure, use a fault model such as
`--models fake:flaky --checks A1`. The fixture also accepts fault modes via
`--mode`. The endpoint must be loopback HTTP on an explicit port other than
`11434`.

Copy `canary.toml.example` to `canary.toml` to configure the default attempt
count, output directory, deadlines, stall timeout, and thinking-capable
models. The defaults are a 120-second deadline and a 30-second stall timeout.
Explicit command-line settings take precedence.

Each run writes a UTC-stamped `results/*_endpoint.json` by default. It records
model digests from `/api/tags` when available, plus the exact request JSON,
response body or timestamped SSE lines, and machine-readable reasons for each
attempt. Exit status is 0 for all pass, 1 for any non-pass check, and 2 for
an infrastructure or usage error.

## Managed Ollama usage

Use `--ollama last:2` for recent stable releases or `--ollama 0.40.16,0.40.15`
for specific versions. Use `--dry-run` to resolve releases and show the download
and model pull plan and free disk space without writing state or starting a server.
`GITHUB_TOKEN` is optional for GitHub API requests.

The managed workflow keeps extracted runtimes, process homes,
logs, and model stores under `CANARY_HOME`, which defaults to `~/.stack-canary`.
It pulls missing models into a master store. Each version runs sequentially
with an isolated clone on loopback port 11535 by default. On macOS the model
files use APFS copy-on-write clones. Set `allow_full_copy = true` to explicitly
permit a full copy on macOS. Other platforms use full copies. The free-space
reserve defaults to 5 GiB.

The optional `seed_store` setting is unset by default. On macOS, opt in with
`seed_store = "~/.ollama/models"` to clone missing files into the master store
using APFS. The source is read-only. Once a runtime passes archive verification
and installation, Canary removes the downloaded tarball.

The v0.1 model set is `llama3.2:3b` (Llama-3 JSON template),
`granite4:3b-h` (Granite tool format), and `qwen3.5:4b`. The first two are
already in the Mac mini's canary store. The installed `tev1:4b` was dropped
because it reports only `decision` in `/api/show`.

Managed runs write one schema-version-2 JSON file per release under
`$CANARY_HOME/results`. A relative `--results-dir` is relative to `CANARY_HOME`.
Reports include base and derived model digests, capabilities, GPU offload,
log matches, and check attempts. The check cells can be `pass`, `fail`,
`timeout`, `flaky`, `n/a`, or `error`. Models that require a newer Ollama
version are `n/a`. When the base model lacks the `tools` capability, all
its checks are `n/a` with reason `model_lacks_tools` and it skips warm-up.
The capability check also applies to `qwen3.5:4b`.
Warm-up and GPU errors, including zero GPU offload, produce `error` cells
and a `model_error` with HTTP status and up to 2 KiB of response body when
available. The runner checks `/api/version` and moves to the next model
if the server answers. An `error` or infrastructure failure gives exit code
2, a failed, timed-out, or flaky check gives 1, and pass and `n/a` alone give 0.

The managed runner prints a line for each heavy step and records before and
after samples in the result's `disk_trace`. Each sample includes free disk
bytes and macOS swap usage in bytes. Swap is `null` elsewhere or when the
reading is unavailable.

The server's log diagnostics include `stop_note` when shutdown encounters
a problem. If Ollama still holds the configured port after the shutdown
grace period, the runner reports `stop_failed` or preserves the original
failure with a shutdown note.

Managed runs compare read-only fingerprints of the existing Ollama store and
its loopback API, plus the existing Hermes configuration and service metadata.
A file or API change is reported as `isolation_violated` and gives exit code 2.
Launchd and listener changes are warnings. Hermes configuration and `.env`
are compared by SHA-256.

## Live smoke test (macOS)

From the repository on the Mac mini, preview the plan, then run the newest
stable release with the v0.1 model set.

```sh
canary run --ollama last:1 --models llama3.2:3b,granite4:3b-h,qwen3.5:4b --dry-run
canary run --ollama last:1 --models llama3.2:3b,granite4:3b-h,qwen3.5:4b
```

Check the versioned JSON file for the Ollama version, nonzero GPU offload,
and the five Layer A outcomes.

## Hermes Layer B

Add `--hermes latest` to include B6. Use `last:N` or a stable tag such as
`v0.23.1` to select specific Hermes releases. Layer A and B6 run by default
with this option. Use `--checks B6` to run just the harness check.

```sh
canary run --ollama last:1 --hermes latest --models llama3.2:3b --checks A1,A2,A3,A4,A5,B6
```

The managed installer syncs a tagged Hermes checkout into a dedicated
virtual environment using its lockfile and an editable source install. It
reads the required Python minor from `.python-version` or the Hermes PM lock,
then verifies an interpreter from the parent PATH. If needed, uv installs
that minor inside `CANARY_HOME/uv-python`. The install uses the interpreter's
absolute path and records the exact version in the harness metadata.

For Hermes v0.23.2, the B6 one-shot gateway accepts the following options
when advertised by the installed command's help. Canary uses the absolute
launcher path in its private Hermes virtual environment.

```sh
"$CANARY_HERMES" chat --oneshot --provider custom --base-url "$PROXY_URL" -m "$MODEL" -t file \
  --query-file "$QUERY" -Q --format stream-json --safe-mode \
  --api-key stack-canary-loopback --reasoning none --max-turns 8 \
  --yolo --source tool --in "$WORKDIR"
```

The gateway's [v0.23.2 option validation](https://github.com/NousResearch/hermes-agent/blob/v0.23.2/hermes_cli/gateway_chat.py#L13-L31)
rejects `--run-budget`. Canary enforces its own deadline and cleanup. A CLI
rejection before the first proxied request is `error` with reason
`harness_cli_rejected` and attribution `canary`. Other failures before the
first request retain harness attribution and record `before_first_request`.

B6 uses an isolated home, a fresh work directory, and a recording loopback proxy
forwarding to managed Ollama. It verifies the exact file content and checks
for a `write_file` event when Hermes supports stream JSON. Failed attempts
report whether the recorded upstream responses were broken. Cleanup targets
the launched process group and canary-owned process paths.

## Compatibility report and repro drafts

```sh
canary report
canary report --results ~/.stack-canary/results results --out report/index.html --repro-dir repro
```

The default reads results from `$CANARY_HOME/results` and the repository's
`results/` directory. Open `report/index.html` in a browser. Rows are Ollama
versions, newest first. The newest managed run determines which models are
shown. Use `--all-models` to include historical models. Each model has A1–A5
columns and one B6 column per Hermes tag. The dots show the recorded attempts. A regression marks a cell
that passed on the next older Ollama version and now has another outcome.
The matrix keeps the newest actual result per cell and fills gaps with any
recorded infrastructure error.

`fail`, `flaky`, and `timeout` cells link to local Markdown drafts under
`repro/`. The drafts replay the recorded request against a separate Ollama
store and include the last older version that passed when available. The
report removes stale drafts bearing its ownership marker and leaves
unmarked files intact. Run
`canary report` from the repository root to use the default output
directories. Both output directories are Git-ignored.

See [the project checklist](tasks/todo.md) for planned work.
