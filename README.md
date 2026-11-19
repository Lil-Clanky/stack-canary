<p align="center">
  <a href="https://lil-clanky.github.io/stack-canary/"><img src="assets/banner.svg" width="100%" alt="Stack Canary: every Ollama release, tested before your agent meets it"></a>
</p>

<p align="center">
  <a href="https://github.com/Lil-Clanky/stack-canary/actions/workflows/test.yml"><img src="https://github.com/Lil-Clanky/stack-canary/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="https://lil-clanky.github.io/stack-canary/"><img src="https://img.shields.io/badge/matrix-live-facc15?labelColor=0b1120" alt="Live matrix"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-94a3b8?labelColor=0b1120" alt="MIT license"></a>
</p>

Ask `qwen3.5:0.8b` on Ollama 0.40.16 to save one line of TypeScript through a
tool call, and this is what arrives in the file:

```diff
- type Box<T=any> = { value: T };
+ type Box<T name="any"> = { value: T };
```

Nothing fails. The tool call is well formed, `finish_reason` is `tool_calls`,
and the code is wrong. The Qwen 3.5 parser reads `<T=any>` as a tag with an
attribute and rewrites it ([ollama/ollama#19694](https://github.com/ollama/ollama/issues/19694)).
An agent that writes generics, C++ templates or JSX through it saves broken
files and never finds out.

Bugs like this sit between the model and your agent: in the chat template,
the tool-call parser, the stream. Any Ollama release can change them, and your
agent's own tests will not notice. Stack Canary installs each new release next to your own
Ollama, sends every model the same seven checks, and keeps the exact request
and response. When a release changes the result, you get a red cell and a
`curl` command that replays it.

## Latest run

<p align="center">
  <a href="https://lil-clanky.github.io/stack-canary/"><img src="https://raw.githubusercontent.com/Lil-Clanky/stack-canary/matrix/matrix.svg" alt="Compatibility matrix of the latest run: one row per Ollama version and model, one column per check"></a>
</p>

This image is redrawn after every run. The [live page](https://lil-clanky.github.io/stack-canary/)
shows each attempt, why it failed, and the repro for every red cell.

## The checks

| | Sends | Passes when |
| --- | --- | --- |
| A1 | A plain chat message | the reply says CANARY and stops normally |
| A2 | The same, streamed | the stream says CANARY and ends with `[DONE]` |
| A3 | A `get_weather` tool | the call comes back as a tool call for Amsterdam, with no tool markup in the text |
| A4 | The same, streamed | the streamed pieces add up to that call |
| A5 | A tool result | the model uses it, answers 17, and stops calling tools |
| A6 | A `write_file` tool | `type Box<T=any> = { value: T };` arrives unchanged |
| B6 | A real agent, [Hermes](https://github.com/NousResearch/hermes-agent) | the file it writes is exactly right |

Each check runs three times per model. They test the plumbing, not how smart
the model is.

## Try it

You need macOS for the managed runs and Python 3.11 or newer. Stack Canary has
no dependencies.

```sh
git clone https://github.com/Lil-Clanky/stack-canary && cd stack-canary
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

canary run --ollama last:2 --models qwen3.5:0.8b --checks A3,A6 --context-length 8192
canary report && open report/index.html
```

This downloads the two newest Ollama releases, pulls the model if you do not
have it yet, and runs each release on port 11535 with its own copy-on-write
clone of the model files. Your own Ollama on 11434
keeps running. Afterwards an audit checks that your Ollama, its models and
your launch agents are exactly as they were. Add `--dry-run` to see what it
would download first.

Not on a Mac? Point it at any OpenAI-compatible server you started yourself:

```sh
canary run --endpoint http://127.0.0.1:8080 --models qwen3.5:0.8b
```

To run it every night and test only what is new:

```sh
canary schedule install --at 04:30
```

Add `notify = ["macos"]` to `canary.toml` to hear about it when a cell turns
red.

## Made for a 16 GB Mac

Stack Canary runs on a 16 GB Mac mini that once reset itself while a model ran
with a 64K context. It now checks memory pressure and swap before it starts
anything, and a watchdog stops Ollama before the Mac stalls. A run stopped
that way counts as `error`, never `fail`, so a busy Mac never shows up as an
Ollama regression.

## More

- [Reference](docs/reference.md): managed runs, the Hermes check, the report,
  scheduling, publishing your own matrix.
- [Project log](tasks/todo.md): what is done, what is open, and the
  measurements behind it.
- Working on it: `pip install -e ".[dev]" && pytest` runs the tests against a
  fake Ollama server, so no models are needed.

MIT license.
