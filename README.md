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

The managed workflow keeps extracted runtimes, process homes,
logs, and model stores under `CANARY_HOME`, which defaults to `~/.stack-canary`.
It pulls missing models into a master store. Each version runs sequentially
with an isolated clone on loopback port 11535 by default. On macOS the model
files use APFS copy-on-write clones. Set `allow_full_copy = true` to explicitly
permit a full copy on macOS. Other platforms use full copies. The free-space
reserve defaults to 5 GiB.

On macOS, set `seed_store = "~/.ollama/models"` to clone missing files into
the master store using APFS. The source is read-only. Once a runtime passes
archive verification and installation, Canary removes the downloaded tarball.

The v0.1 model set is `llama3.2:3b` (Llama-3 JSON template),
`tev1:4b` (Qwen3.5 tool parser), and `granite4:3b-h` (Granite tool format).
It covers llama.cpp with GGUF and MLX with tev1's safetensors.

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

For the three-model smoke test, opt in to `seed_store` in `canary.toml`, then run:

```sh
canary run --ollama last:1 --models llama3.2:3b,tev1:4b,granite4:3b-h --dry-run
canary run --ollama last:1 --models llama3.2:3b,tev1:4b,granite4:3b-h
```

## Hermes Layer B

Add `--hermes latest` to include B6. Use `last:N` or a stable tag such as
`v0.23.1` to select specific Hermes releases. Layer A and B6 run by default
with this option. Use `--checks B6` to run just the harness check.

```sh
canary run --ollama last:1 --hermes latest --models llama3.2:3b --checks A1,A2,A3,A4,A5,B6
```

The managed installer creates a dedicated Hermes virtual environment. B6 uses
an isolated home, a fresh work directory, and a recording loopback proxy
forwarding to managed Ollama. It verifies the exact file content and checks
for a `write_file` event when Hermes supports stream JSON. Failed attempts
report whether the recorded upstream responses were broken. Cleanup targets
the launched process group and canary-owned process paths.

See [the project checklist](tasks/todo.md) for planned work.
