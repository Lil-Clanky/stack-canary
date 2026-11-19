# Stack Canary reference

The details behind the [README](../README.md): managed Ollama runs, memory
safety, the Hermes check, the report, scheduling and publishing.

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

Before each server start, Canary runs the binary once with `--help`. On macOS,
Gatekeeper holds the first launch of a binary from a new path until it has
checked it, which includes an online notarization lookup. On a slow network
this took 11 minutes for Ollama 0.40.16 on an external disk. The wait prints
a line, does not count against the 60-second readiness timeout, and gives up
after 30 minutes with `launch_timeout`. Gatekeeper keeps its verdict for that
path, so the next run starts at once. Moving `CANARY_HOME` makes every runtime
a new path again.

The optional `seed_store` setting is unset by default. On macOS, opt in with
`seed_store = "~/.ollama/models"` to clone missing files into the master store
using APFS. The source is read-only. Once a runtime passes archive verification
and installation, Canary removes the downloaded tarball.

The model set is `llama3.2:3b` (Llama-3 JSON template),
`granite4:3b-h` (Granite tool format), and `qwen3.5:0.8b` (Qwen 3.5 parser).
`qwen3.5:4b` was replaced because loading it raised macOS memory pressure to
the warning level on the 16 GB Mac mini. The 0.8b model uses the same parser
and renderer.

Managed runs write one schema-version-2 JSON file per release under
`$CANARY_HOME/results`. A relative `--results-dir` is relative to `CANARY_HOME`.
Reports include base and derived model digests, capabilities, GPU offload,
log matches, and check attempts. The check cells can be `pass`, `fail`,
`timeout`, `flaky`, `n/a`, or `error`. Models that require a newer Ollama
version are `n/a`. When the base model lacks the `tools` capability, all
its checks are `n/a` with reason `model_lacks_tools` and it skips warm-up.
The capability check also applies to `qwen3.5:0.8b`.
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

## Memory safety

A 16 GB Mac mini reset itself while an earlier version ran B6 with a 64K
context. Managed runs on macOS now guard memory:

- A server start, model load or check is refused with `memory_pressure` when
  macOS reports memory pressure at the warning level or higher, when swap use
  exceeds `max_swap_gb` (default 4), or when the Ollama on port 11434 still
  has models loaded.
- While a server runs, a watchdog checks once per second. It kills the server
  when pressure reaches the critical level or swap exceeds `max_swap_gb`.
  Attempts cut off this way are discarded, and the cell becomes `error`, never
  `fail`.
- Layer A needs little context. Use `--context-length 8192` for runs without
  B6. B6 requires at least 64000 tokens, the Hermes minimum.
- A watchdog stop during `canary hermes verify` is inconclusive and leaves the
  registry unchanged. On the 16 GB Mac mini, the `llama3.2:3b` probe at 64K
  reached critical pressure with a browser and other apps open, and the
  watchdog stopped it. Close heavy apps before B6 runs.

## Live smoke test (macOS)

From the repository on the Mac mini, preview the plan, then run the newest
stable release with the default model set.

```sh
canary run --ollama last:1 --models llama3.2:3b,granite4:3b-h,qwen3.5:0.8b --dry-run
canary run --ollama last:1 --models llama3.2:3b,granite4:3b-h,qwen3.5:0.8b --context-length 8192
```

Check the versioned JSON file for the Ollama version, nonzero GPU offload,
and the six Layer A outcomes.

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

On macOS, B6 requires a verified tag and matching installed commit. Canary
seeds `CANARY_HOME/hermes-verified.json` with the previously audited
v0.23.2 commit. Run `canary hermes verify v0.23.2` (or another explicit
stable tag) to update the record. The verifier checks the explicit endpoint
and real-home handling in source, then runs one Llama B6 against the newest
tested Ollama with a logging launchctl shim and a before/after isolation
audit. A tag with a failed probe is marked unsupported. Only a newly
registered launchd label whose program or Hermes home resolves inside
Canary is eligible for probe cleanup. The foreground path requires a
Canary home short enough to fit its Unix control socket.

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
`results/` directory. Open `report/index.html` in a browser. A card at the
top lists what did not pass on the newest tested release. Rows are grouped by
Ollama version, newest first, with one row per model. The configured `models`
are shown, or the models of the newest managed run when none of them has
results. Use `--all-models` to include historical models. A model without a
result for a version is left out of that group. The columns are A1–A6 and one
B6 column per Hermes tag. The dots show the recorded attempts.
A regression marks an A-check or runtime-attributed B6 result that fails, is
flaky or times out after passing on the next older Ollama version. Errors and
`n/a` are never regressions. Harness failures have a separate marker.
The matrix keeps the newest actual result per cell and fills gaps with any
recorded infrastructure error.

`fail`, `flaky`, and `timeout` cells link to local Markdown drafts under
`repro/`. The drafts replay the recorded request against a separate Ollama
store and include the last older version that passed when available. The
report removes stale drafts bearing its ownership marker and leaves
unmarked files intact. Run
`canary report` from the repository root to use the default output
directories. Both output directories are Git-ignored.

## Scheduling

`canary watch` uses the managed run lock and checks the newest two stable
Ollama releases by default. For each release without a complete set of
results for the configured models and pinned Hermes tag, it runs only the
models and checks that are missing. A newly added check or model therefore
runs alone and does not repeat B6 for the others. Results can come from separate eligible files: watch uses the report's
cell merge. On macOS, watch verifies a newly discovered latest stable Hermes
tag and runs a separate B6 column for a verified newer tag. A failed probe
appears in the summary. Use `canary watch --dry-run` to preview the plan
without probing. Release discovery uses the existing 60-minute cache.

After new work, the watcher regenerates the compatibility report and repro
drafts, writes `report/summary.md` and `report/summary.json`, and prunes old
installations. It also writes a summary when there is nothing new. The summary
records regressions, infrastructure and isolation errors, and resource
measurements from the run. Exit codes are 0 for no new checks or all passes,
1 for a check that did not pass, and 2 for infrastructure or isolation errors.

`canary gc --dry-run` previews cleanup. `canary gc` keeps the newest three
Ollama runtimes and their clones, and the pinned Hermes tag plus the newest
two Hermes installs by default. Results, the master model store, and shared
uv data are retained. Space freed is the measured change in free disk space,
which accounts for shared blocks in APFS clones.

The optional `notify_command` is a list of arguments. Canary runs it with a
15-second timeout, appending the Markdown summary path. It runs on a new
regression, an infrastructure or isolation error, or a newly discovered
unverified Hermes tag. Set `notify_on_success = true` to also notify on success.
Configure the watcher in `canary.toml` using `canary.toml.example`.

Built-in notifications can be enabled with `notify = ["macos", "ntfy"]`.
Set a private `ntfy_topic` in the ignored local configuration. The optional
`ntfy_server` defaults to `https://ntfy.sh`. Messages contain a fixed
headline and incident counts rather than raw reports. Notification failures
do not change the run's exit code. Use `canary notify --test` to check the
configured channels. It submits real test notifications.

## Publishing the matrix

`canary report` writes `report/MATRIX.md` and `report/matrix.svg` alongside
its default HTML output. The Markdown matrix uses one B6 column per Hermes
tag, marks runtime regressions and links to repro drafts. The SVG is the same
matrix as one image, for places that do not allow HTML, such as a README.

Set `publish_branch = "matrix"` and `publish_gh_user` to your GitHub account
to enable publishing to the `matrix` branch of your repository. Run
`canary publish` manually, or let watch publish when configured. The
publisher uses an orphan worktree at `CANARY_HOME/publish` and pushes only
the matrix ref. It allows only the three matrix formats, the two summaries
and generated repro drafts. It shortens macOS user paths and refuses to
push detected credentials, the ntfy topic or the proxy API key. GitHub
authentication is scoped to the configured account without changing
global Git or gh settings. A failed push is retried twice, 30 seconds
apart, because a short network drop is enough to fail it. If all three
attempts fail, the commit stays in the worktree and the next publish pushes it.

On macOS, run the following from the repository with Canary installed in
`.venv`. The time is local.

```sh
canary schedule install --at 02:15 --print
canary schedule install --at 02:15
canary schedule status
canary schedule uninstall
```

Use `--replace` to update an existing schedule. The job runs `.venv/bin/canary
watch` from the repository, logs to `~/.stack-canary/logs/watch.log` by default,
and uses the configured `CANARY_HOME` when set. Its writes are confined to the
canary home and the repository's `report/` and `repro/`.

Only explicit `schedule install` and `schedule uninstall` commands change
launchd state. They manage the single `com.stackcanary.watch` label and
`~/Library/LaunchAgents/com.stackcanary.watch.plist`. The isolation audit
records this job and plist and excludes both from its violation diff.
