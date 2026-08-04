# Task J — browser-child startup handshake covers every pre-readiness failure

*2026-08-04. Decision: `BUILDLOG.md` D23.*

**Success criteria:** any failure in the browser child before the shim is ready — Playwright
driver startup, `chromium.launch()`/`launch_persistent_context()`, context/page creation, not
just `page.goto`/`_wait_for_shim` — reaches the parent as a prompt startup error, not as a
60 s "failed to become ready" timeout. Exactly one message is written to `startup_queue` per
child lifetime; a post-startup failure is logged, never queued (D23 rationale: a stray message
would be misread by a future reader of the queue).

Code: `src/voicebox/browser_session.py` `_run_browser_async` — wraps the whole child body in a
`started` flag + `try/except/finally`; every handle (`browser`/`context`/`page`) defaults to
`None` and teardown is guarded per-handle so a failure before page creation doesn't crash on
`page.on(...)` or `context.close()` against a `None`.

## Live proof — real spawned child, real Chromium launch failure

`test_chromium_launch_failure_propagates` (`tests/test_browser_session.py`) sets
`PLAYWRIGHT_BROWSERS_PATH` to an empty temp dir before calling `start_browser` — the env is
inherited across `spawn`, so the *real* spawned child's `chromium.launch()` raises
"Executable doesn't exist" before any page exists. No mocking of `_run_browser_async` itself.

```
$ uv run pytest tests/test_browser_session.py -v -k launch_failure
tests/test_browser_session.py::test_chromium_launch_failure_propagates PASSED [100%]
1 passed, 5 deselected in 0.53s
```

Assertions: `RuntimeError` raised with "startup failed" in the message (not "failed to become
ready" — the parent's readiness-timeout fallback text never fires); elapsed wall-clock < 30 s
against a `startup_timeout=60.0` (the child reported instead of the parent waiting out the
full timeout); the child process is no longer alive; parent-side session state
(`_browser_process`, `_startup_queue`, `_stop_event`) is cleared, matching the existing
`test_navigation_failure_leaves_no_process` contract for post-goto failures.

## Test suite

```
$ uv run pytest -q
82 passed, 5 warnings in 57.18s
```

Lint/format/types: `ruff check src/ tests/` — All checks passed; `ruff format --check` — 24
files already formatted; `pyright src/` — 2 errors, both pre-existing on HEAD before this
change (same 2 as recorded in Task I's artefact: `agent.py:534` optional-member,
`browser_session.py:37` `multiprocessing.Event` type annotation — unrelated to this diff, not
introduced by it).

## Not covered

- **A failure between page creation and `page.goto`** (e.g. `add_init_script` raising) is
  covered by code inspection — the same `try/finally` wraps it and `page` is non-`None` by
  then, so `_dump_shim_diag` runs — but not exercised by a dedicated test; the existing
  `test_navigation_failure_leaves_no_process` covers the `page.goto`/`_wait_for_shim` failure
  path specifically.
- **A post-startup child crash** (`started=True`, then an exception in the park loop) is
  logged-not-queued by design (see rationale above); not exercised live — would require
  killing Chromium mid-session and asserting the parent's *existing* `stop_event`/liveness
  handling is unaffected, which was already true before this change and is out of this task's
  scope.
- **SIGKILL/OOM child death** (no Python exception handling runs at all) is explicitly
  rejected as out of scope in `BUILDLOG.md` D23 — the queue write only covers exception paths,
  not signals.
