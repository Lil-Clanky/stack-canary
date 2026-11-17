# Stack Canary lessons

Verify a candidate model's capabilities with `/api/show` on the target machine before adding it to the model set; the library listing can differ from the installed model (tev1:4b reports only `decision`).

On macOS, `killpg` can return EPERM instead of ESRCH (e.g. a group of only zombies); treat both as 'gone' and verify shutdown by the port, not by signals. Never let cleanup errors mask the original exception.

An option listed in `--help` is not proof it is accepted in every mode. Classify our own invocation errors (usage exit codes, zero upstream requests) as `error`, never as an upstream failure with a repro.
