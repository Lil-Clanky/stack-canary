# Stack Canary

Stack Canary checks protocol compatibility across releases of local LLM stacks.
The project targets Ollama and Hermes Agent so that changes such as broken tool
calls or hanging streams can be detected and reported by version, model, and
check. It measures protocol behavior.

**Status:** v0.1 in progress. The command-line skeleton and outcome aggregation are available.

```sh
python -m pip install -e ".[dev]"
python -m pytest
canary report
```

See [the project checklist](tasks/todo.md) for planned work.
