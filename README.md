# Stack Canary

Stack Canary checks protocol compatibility across releases of local LLM stacks.
The project targets Ollama and Hermes Agent so that changes such as broken tool
calls or hanging streams can be detected and reported by version, model, and
check. It measures protocol behavior.

## Status v0.1

The full Mac mini matrix passed all 108 attempts across Ollama 0.40.16 and
0.40.15, three models, and checks A1–A5 and B6. Hermes v0.23.2 ran B6 in
foreground mode. See the [review](tasks/todo.md#review) for isolation evidence,
runner and GPU measurements, and known limitations.

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

Managed runs take a read-only baseline before release resolution and compare it
with a final snapshot after cleanup. The audit covers the existing Ollama store
and its loopback API, Hermes configuration, and service metadata. File or API
changes, added or removed relevant launchd labels, and changes under
`~/Library/LaunchAgents/*.plist` are reported as `isolation_violated` with
exit code 2. The label comparison is case-insensitive and matches names
containing `hermes`, `ollama`, `nousresearch`, or `canary`. PID changes for
existing matching jobs and listener changes are warnings.
Hermes configuration and `.env` are compared by SHA-256. The config diff also
lists changed YAML mapping keys without their values.

Managed runs cache GitHub release listings for 60 minutes under
`CANARY_HOME/release-cache`. Requests use `GITHUB_TOKEN` when set and fall back
to `gh auth token` when available. Dry runs can reuse a fresh listing and leave
the cache untouched.

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

Add `--hermes v0.23.2` to include B6 on macOS. The selectors `latest` and
`last:N` also resolve stable Hermes releases. Layer A and B6 run by default
with this option. Use `--checks B6` to run just the harness check.

```sh
canary run --ollama last:1 --hermes v0.23.2 --models llama3.2:3b --checks A1,A2,A3,A4,A5,B6
```

The managed installer syncs a tagged Hermes checkout into a dedicated
virtual environment using its lockfile and an editable source install. It
reads the required Python minor from `.python-version` or the Hermes PM lock,
then verifies an interpreter from the parent PATH. If needed, uv installs
that minor inside `CANARY_HOME/uv-python`. The install uses the interpreter's
absolute path and records the exact version in the harness metadata.

On macOS, Canary starts `hermes gateway run --force` as a foreground child
for each B6 attempt. Each attempt has an isolated profile and host lock
directory. Chat attaches to the foreground child's discovered endpoint via
Hermes' explicit client path. If the child exits, chat cannot autostart a
replacement or register a launchd job. Canary sets `HERMES_REAL_HOME` to its
own `HOME`, outside the profile's special `home` directory, so Hermes'
subprocess home resolver keeps the isolated path.

The JSON records `gateway_mode: foreground` for the audited v0.23.2 commit.
Other macOS releases get B6 `error` with reason `harness_requires_launchd`
until their launch path is verified. The foreground path requires a canary
home short enough to keep the Unix control socket inside it.

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
first request are `error` with harness attribution and the note
`before_first_request`. A gateway that never reaches Ollama reports
`harness_not_started`.

B6 uses an isolated home, a fresh work directory, and a recording loopback proxy
forwarding to managed Ollama. It verifies the exact file content and checks
for a `write_file` event when Hermes supports stream JSON. Attribution is
computed for non-pass attempts. A non-2xx or malformed chat completion,
including an SSE stream missing `[DONE]`, is attributed to the runtime.
Probe responses such as `GET /api/v1/models` returning 404 are informational.
The proxy records exception details for upstream errors and client disconnects.
Cleanup targets the launched process group and canary-owned process paths.

## Compatibility report and repro drafts

```sh
canary report
canary report --results ~/.stack-canary/results results --out report/index.html --repro-dir repro
```

The default reads results from `$CANARY_HOME/results` and the repository's
`results/` directory. Open `report/index.html` in a browser. Rows are Ollama
versions, newest first. The newest managed run determines which models are
shown. Use `--all-models` to include historical models. Each model has A1–A5
columns and one B6 column per Hermes tag. The dots show the recorded attempts.
A regression marks an A-check or runtime-attributed B6 result that passed on
the next older Ollama version. Harness failures have a separate marker.
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
