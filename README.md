# Stack Canary

Stack Canary checks protocol compatibility across releases of local LLM stacks.
The project targets Ollama and Hermes Agent so that changes such as broken tool
calls or hanging streams can be detected and reported by version, model, and
check. It measures protocol behavior.

**Status:** v0.1 in progress. Layer A runs five protocol checks in endpoint or managed Ollama mode. The managed workflow has been tested with the bundled fake Ollama. `canary report` prints `not implemented yet`.

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

The managed workflow keeps verified downloads, extracted runtimes, process homes,
logs, and model stores under `CANARY_HOME`, which defaults to `~/.stack-canary`.
It pulls missing models into a master store. Each version runs sequentially
with an isolated clone on loopback port 11535 by default. On macOS the model
files use APFS copy-on-write clones. Set `allow_full_copy = true` to explicitly
permit a full copy on macOS. Other platforms use full copies. The free-space
reserve defaults to 5 GiB.

Managed runs write one schema-version-2 JSON file per release under
`$CANARY_HOME/results`. A relative `--results-dir` is relative to `CANARY_HOME`.
Reports include base and derived model digests, GPU offload, log matches, and
check attempts. A model whose minimum Ollama version is newer than the
release under test is marked `n/a`. Zero GPU offload is an infrastructure
error. The run writes the affected release's report and continues with the
remaining releases.

## Live smoke test (macOS)

From the repository on the Mac mini, preview the plan, then run the newest
stable release with one model.

```sh
canary run --ollama last:1 --models llama3.2:3b --dry-run
canary run --ollama last:1 --models llama3.2:3b
```

Check the versioned JSON file for the Ollama version, nonzero GPU offload,
and the five Layer A outcomes.

See [the project checklist](tasks/todo.md) for planned work.
