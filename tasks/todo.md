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

The full Mac mini run on `93ab93a` covered Ollama 0.40.16 and 0.40.15,
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
- [ ] Test the optional schedule on the Mac with the user's explicit approval
  to create the LaunchAgent in their domain.

## Phase 8

- [x] Share the managed run lock and recover stale owners.
- [x] Watch the release listings and run only incomplete Ollama versions.
- [x] Write nightly summaries, regenerate reports, and support an optional
  notification command.
- [x] Prune old managed releases while keeping the master store and results.
- [x] Implement the opt-in macOS schedule and record its exact audit exemption.
- [x] Cover the scheduler with offline launchd mocks.


## Review 0.3.0 (development)

- The macOS foreground registry binds B6 eligibility to a tag and commit.
- The v0.23.2 re-verification passed the static contract and the B6 attempt
  on Ollama 0.40.16. The launchctl shim was empty and no new relevant job
  or plist appeared. The broader isolation audit reported a violation, so
  the live attempt did not renew the attestation. Record a clean
  re-verification before releasing.
- macOS accepted the test notification submission, and the ntfy server
  accepted its test with HTTP 200.
- The publish command sanitizes the compatibility artifacts and restricts
  its push to the orphan matrix branch.
- Watch uses the merged history for split A/B6 coverage. The offline suite
  covers the merge and rejects B6 evidence from an isolation violation.
- A new full watch and release check are pending until the Mac is available
  for local model workloads.

## Review 2026-11-18: memory guard and first red cell

- The Mac mini reset itself on 2026-11-17 at 21:58 (`ResetCounter`, boot fault
  `wdog`) during Canary work with a 64K context. Ollama logs from that evening
  show `free_swap="0 B"`, swap near 7 GB and free disk falling from 10.4 to
  7.7 GB. The exact trigger is not proven.
- Managed runs now refuse a server start, model load or check under macOS
  memory pressure (warning or worse), above `max_swap_gb` (default 4) or while
  the Ollama on port 11434 has models loaded. A watchdog kills the server at
  critical pressure or above `max_swap_gb`. Cut-off attempts become `error`.
- `--context-length` overrides the context for a run. B6 refuses less than
  64000 tokens (Hermes `MINIMUM_CONTEXT_LENGTH`).
- New check A6 sends a `write_file` call whose content is
  `type Box<T=any> = { value: T };`. On Ollama 0.40.16, `qwen3.5:0.8b` failed
  3 of 3 attempts with `type Box<T name="any"> = ...` (ollama/ollama#19694,
  case 1). A3 passed on the same model. `llama3.2:3b` and `granite4:3b-h`
  passed A6 on 0.40.16 and 0.40.15.
- `qwen3.5:4b` raised memory pressure to the warning level at load, so the
  guard refused A6. The default model set now uses `qwen3.5:0.8b`.
- Back-tests that found nothing: `llama3.2:3b` on Ollama 0.7.1 and 0.8.0 passed
  A1–A5 (0.7.1 buffers a streamed tool call into one valid chunk). Qwen 3.5 on
  0.17.5 and 0.17.6 could not be tested: the registry refuses current Qwen 3.5
  manifests to those versions (HTTP 412).
- Watch now runs only missing checks, so adding A6 does not repeat B6.
- Errors and `n/a` no longer count as regressions in the report or summary.

### Open

- [x] Free disk space: about 18 GB free later on 2026-11-18.
- [x] Fill Layer A for `qwen3.5:0.8b`: A1–A5 pass and A6 fails 3/3 on both
  0.40.16 and 0.40.15 (clean isolation audit on the second run; the first was
  flagged because the user's own Ollama restarted and dropped a partial pull).
- [ ] B6 for `qwen3.5:0.8b`: blocked. Hermes v0.23.2 is `unsupported` in the
  registry. The 2026-11-17 probe passed, but its isolation audit was flagged
  without a recorded cause (now recorded as `isolation_changes`). The rerun on
  2026-11-18 hit critical memory pressure with `llama3.2:3b` at 64K, and the
  watchdog stopped it (now inconclusive instead of `unsupported`). Rerun
  `canary hermes verify v0.23.2` with heavy apps closed.
- [ ] Add a second agent harness (pi or opencode) next to Hermes.
- [ ] CI on `macos-latest`: 15 managed-runtime tests time out waiting for the
  fake Ollama (`readiness_timeout` after 4 s) although the master server in the
  same test starts. They pass on the local Mac. CI runs on Linux until then.
- [x] Make the repository public and publish the matrix on GitHub Pages
  (2026-11-18).
- [ ] With approval: add the end-to-end confirmation to ollama/ollama#19694.
- [x] Redesign the report page, add `matrix.svg`, and rebuild the README
  around the banner, the live matrix image and the how-it-works diagram. The
  reference text moved to `docs/reference.md` (2026-11-19).
- [ ] The scheduled watch of 2026-11-19 could not start Ollama for the missing
  B6 cells (`Ollama did not become ready in 60s` on 0.40.16 and 0.40.15), and
  its publish push failed with git exit 128. A manual `canary publish` later
  that day worked. Cause not found; the canary home is now on an external
  volume.
- [ ] Upload `assets/social-preview.png` under Settings → Social preview.
  GitHub has no API for it.
