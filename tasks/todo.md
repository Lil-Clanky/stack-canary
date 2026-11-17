# Stack Canary phases

- [x] 1. Skeleton.
- [x] 2. Layer A against a fake Ollama.
- [x] 3. Ollama runtime manager (code).
  - [x] Resolve stable releases in numeric version order and verify SHA-256.
  - [x] Stage and safely extract each version into its own runtime tree.
  - [x] Run an isolated loopback server with a dedicated home, clean environment, and logs.
  - [x] Prepare a master model store, create deterministic derived models, and clone the store per version.
  - [x] Warm up models, record GPU offload, and write one result file per version.
  - [x] Guard the free-space reserve before installation, pulls, and clones.
  - [x] Phase 3 live smoke test on the Mac mini.
  - [x] Live smoke test with the three v0.1 models.
- [x] 4. Hermes layer B (code). Check B6 writes a file with exact content.
  - Clone the requested tag in isolation and set up a uv virtual environment.
  - Set a dedicated `HERMES_HOME`.
  - Run `hermes chat --oneshot --safe-mode --provider custom ...`.
  - Record traffic with a proxy between Hermes and Ollama.
  - Clean up the gateway after each run.
  - [x] Phase 4 live smoke test on the Mac mini.
- [x] 5. Write one result per version, audit isolation, and draft local curl reproductions for check failures.
- [x] 6. Implement `canary report` as a static `report/index.html` matrix by version, model, and check, with regression markers.
- [x] 7. Run the full Mac mini matrix with 2 Ollama versions, 3 models (`llama3.2:3b`, `granite4:3b-h`, `qwen3.5:4b`), 6 checks, and 3 attempts per check.
  - Cover the Llama-3 JSON template, Granite tool format, and Qwen3.5 when the installed model reports tool support.
  - [x] Layer A passed all 90 live attempts.
  - [x] Full live run with B6.

## Review

The full Mac mini run on `db2ef84` covered Ollama 0.40.16 and 0.40.15,
three models, and six checks (A1–A5 and B6). All 108 attempts passed, with
three attempts per cell. B6 reported `gateway_mode: foreground`.

The earlier Layer A run on the same versions and models recorded the
following runner and GPU offload measurements.

| Model | Runner | GPU offload |
| --- | --- | --- |
| `llama3.2:3b` | `llamacpp` | 100% |
| `granite4:3b-h` | `llamacpp` | 100% |
| `qwen3.5:4b` | `mlx` | 100% |

The full run found no changes to Hermes, Ollama, or Canary launchd labels
and no changed LaunchAgent plists. Spotlight worker turnover triggered
exit code 2 under the previous audit. The audit now limits launchd label
comparison to names containing Hermes, Ollama, Nous Research (`nousresearch`),
or Canary, case-insensitively. It still compares every LaunchAgent plist.

### Known limitations

- B6 on macOS is pinned to the verified Hermes v0.23.2 commit
  (`ae2a81ccc00e9b7c06301523e5bb09608397149c`). Other tags report
  `harness_requires_launchd`.
- `hermes_sees_ollama` was false on the Mac mini because `ollama.localhost`
  did not resolve. The run used `127.0.0.1` for the proxy.
- A 64K context can grow swap on a 16 GB machine.
- A GitHub token is recommended for release lookups to avoid the unauthenticated
  rate limit.

### Next steps

- [ ] Verify newer Hermes tags for macOS foreground mode.
- [ ] Schedule runs as a separate phase with the user's explicit approval.
  Launchd scheduling adds a LaunchAgent in the user's domain. Cron scheduling
  changes the user's crontab.
