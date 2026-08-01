# Task A — startup fails loudly; attach stops destroying the session

*Evidence for the success criteria in
[`docs/specs/2026-07-31-fix-plan-execution.md`](../../specs/2026-07-31-fix-plan-execution.md)
§ "Task A — Session startup fails loudly; attach stops destroying the session".*

Commit: `1df7e51`, landed 2026-07-31.

**Retrospective artefact.** This branch adopted walkthrough discipline after Task A had already
landed (`BUILDLOG.md` D1). Everything below was captured on **2026-08-01** against the committed
state of Task A — it is real output from a real run, not a record of what was seen on the day.

## Criteria → evidence

| # | Criterion | Evidence |
|---|---|---|
| 1 | Failed `goto` raises; no success payload for a blank tab | `test_navigation_failure_propagates` ✅ |
| 2 | Child polls for non-`about:blank` URL **and** `__voiceShim.installed`, bounded 10 s | `browser_session.py:153-176`, `SHIM_READY_TIMEOUT_SECS = 10.0` (`:31`) |
| 3 | `attach_hint` is `playwright-cli attach --cdp …`; old recipe gone from hint, docstring, `CLAUDE.md` | `test_attach_hint_does_not_navigate` ✅ + greps below |
| 4 | `playwright_mcp_env` removed | grep returns nothing |
| 5 | "Do not open new tabs" warning survives in `CLAUDE.md` | grep below |
| 6 | Teardown works when startup raises; no orphaned Chromium | `test_navigation_failure_leaves_no_process` ✅ |

## Test run

```
$ uv run pytest tests/test_browser_session.py -v
collected 3 items

tests/test_browser_session.py::test_navigation_failure_propagates PASSED  [ 33%]
tests/test_browser_session.py::test_navigation_failure_leaves_no_process PASSED  [ 66%]
tests/test_browser_session.py::test_attach_hint_does_not_navigate PASSED  [100%]

============================== 3 passed in 2.74s ===============================
```

`test_navigation_failure_propagates` launches a real headless Chromium at `http://localhost:1/`
(a port nothing binds) and asserts the raise; `test_attach_hint_does_not_navigate` stubs the child
process so no browser is launched.

## Source state

```
$ grep -n "attach_hint\|playwright_mcp_env\|__voiceShim.installed\|about:blank" \
      src/voicebox/browser_session.py src/voicebox/server.py CLAUDE.md

browser_session.py:59:   ``about:blank`` is never reported as a started session.
browser_session.py:97:   "attach_hint": f"playwright-cli attach --cdp {cdp_endpoint}",
browser_session.py:153:  """Poll until the page has navigated away from ``about:blank`` and the shim installed.
browser_session.py:166:  probe = "() => !!(window.__voiceShim && window.__voiceShim.installed)"
browser_session.py:176:  if page.url != "about:blank" and installed:
server.py:91:            The returned ``attach_hint`` is the exact shell command to paste to wire
server.py:95:            which runs ``goto about:blank`` and destroys the audio shim.
server.py:125:           ``{cdp_endpoint, audio_ws_url, attach_hint}``. Raises if the page did
CLAUDE.md:110:           `playwright-cli open` with no URL runs `goto about:blank` on the current page
```

No hit for `playwright_mcp_env` anywhere (criterion 4). No hit for `close-all` or
`PLAYWRIGHT_MCP_ISOLATED` in the hint, the docstring or `CLAUDE.md` (criterion 3).

```
$ grep -n "Do not open new tabs" CLAUDE.md
CLAUDE.md:118:**Do not open new tabs.** The audio shim (`shim.js`) is page-scoped to the
```

## Quality gates

```
$ uv run ruff check src/voicebox/browser_session.py src/voicebox/server.py tests/test_browser_session.py
All checks passed!

$ uv run ruff format --check <same three files>
3 files already formatted

$ uv run pyright src/voicebox/browser_session.py src/voicebox/server.py
  src/voicebox/browser_session.py:35:23 - error: Variable not allowed in type expression
  1 error, 0 warnings, 0 informations
```

The pyright error is **pre-existing, not new**: `Optional[multiprocessing.Event]` annotates a
bound factory method rather than a type. The same file before Task A reports the same class of
error twice (`:30`, `:31`) plus a `reportOptionalMemberAccess` — verified by running pyright over
`git show 1df7e51~1:src/voicebox/browser_session.py`:

```
bs_before.py:30:24 - error: Variable not allowed in type expression
bs_before.py:31:23 - error: Variable not allowed in type expression
bs_before.py:73:25 - error: "wait" is not a known attribute of "None"
3 errors, 0 warnings, 0 informations
```

Task A took that file from 3 errors to 1. The spec's gate is "no *new* errors vs. baseline" — met.

## Not covered

- **A3 🔴** — that `window.__voiceShim.installed` is already `true` when the tool returns, against
  a real app on `localhost:3000`. The poll makes it true by construction, but it has not been run
  against a live voice app.
- **A4 🔴** — that `playwright-cli attach --cdp <endpoint>` + `tab-select 1` + `snapshot` leaves
  the session connected (`client_connected` with no following `client_disconnected`). The claim
  about `open` navigating to `about:blank` is read from playwright-core source
  (`lib/tools/cli-client/program.js:128`), not observed here.
- The 10 s shim-ready timeout is not exercised by any test — only the `goto`-failure path is. A
  page that loads but never installs the shim is untested.
