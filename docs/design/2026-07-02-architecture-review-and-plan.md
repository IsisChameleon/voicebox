# Architecture review & improvement plan — 2026-07-02

*Second full review of voicebox, against the code at `main` (post-Stage-4, pipecat 1.3.0).
Companion to the 2026-06-11 [architecture-review.md](architecture-review.md) and
[upgrade-roadmap.md](upgrade-roadmap.md) — everything in that roadmap through Stage 4 is
merged and verified, so none of it is repeated here. This document is two things:*

1. *a fresh review — what's now good, and the defects/gaps that exist in the current code;*
2. *a delegation-ready task plan: each task has an objective, a contract, acceptance criteria
   and a suggested executor (Sonnet 5 or Opus 4.8), written so a subagent can pick it up
   without re-deriving context.*

---

## Part 1 — Review

### What the architecture gets right (keep these invariants)

- **Three-process split with audio out-of-band.** MCP carries text/control only; audio flows
  over a raw-PCM WebSocket between `shim.js` and the pipecat child. This keeps the MCP request
  path off the audio clock and is the reason `speak`/`listen` latency doesn't degrade with
  utterance length. Do not let any task move audio (or per-chunk events) across the MCP boundary.
- **UI driving delegated over CDP.** voicebox owns no `navigate`/`click` tools and the parent
  holds no Playwright page handle. This is what makes the tool composable with any Playwright
  client. Do not add UI-driving tools to `server.py`.
- **The IPC protocol is now sound.** Correlation-ID'd full-duplex requests, a parent-side
  response router resolving per-id futures, errors on an `error` key, a child loop that survives
  failed commands. The five 2026-06 bugs (B1–B5) are all fixed and verified.
- **The event log is the right abstraction.** One monotonic timestamped log, two clearly-named
  parties (`app_bot` / `tester`), cursor-based `listen`, ground-truth `tester_transcript` at
  speak time, biases (`vad_stop_secs`) recorded in-band. `metrics.py` being a pure function over
  serialized events — with the only unit tests in the repo — is exactly the right pattern; new
  derived data should follow it.
- **Documentation discipline.** `CLAUDE.md` captures verified traps with file/line references;
  design docs are dated and cross-linked; commits reference stages. Every task below must keep
  `CLAUDE.md` in sync when it changes a documented behavior.

### Findings

Ordered by severity. `F#` references are used by the tasks in Part 2.

#### F1 — Live credentials committed to the repository (fix first)

`scripts/e2e_readme_call.py:32-33` hardcoded a real email + password for the readme app's test
account. They are in git history on a public GitHub repo, so they must be treated as
compromised regardless of any repo edit. **Update:** the script has since been deleted outright
(it was coupled to a private app unavailable to repo users, per F9) rather than fixed — see T1.
The leaked password still requires human rotation; history rewriting remains out of scope. Also
of note: `notes.md` at the repo root (pasted conversation scratch, not documentation) has been
deleted too.

#### F2 — No readiness handshake for the pipecat child; first-run failures are opaque

`start_browser_session` returns as soon as the *browser* is ready (`browser_session.py` uses a
`multiprocessing.Event`), but nothing waits for the pipecat child. The child's `create_agent` →
`agent.start()` path synchronously downloads the Kokoro model (~300 MB, `kokoro_tts.py:51-56`)
and loads Whisper on first run. Consequences:

- On a fresh machine, the first `speak`/`listen` sits until the parent-side deadline (60 s /
  `timeout+30`) and then fails with "No response from the voice agent" while the child is
  actually mid-download — the user has no way to tell a hang from a cold start.
- If the child crashes during startup (bad model file, port bound between check and bind,
  import error), `start_browser_session` still returns success; the failure surfaces later,
  attributed to the wrong tool call. The `_response_router` only notices a dead child when a
  command is already pending.

#### F3 — Connection state is write-once; disconnect/reconnect breaks `speak` semantics

`agent.py` sets `self._connected` on `on_client_connected` and never clears it on
`on_client_disconnected`. After a page reload or WS drop:

- `speak()` proceeds immediately and queues Kokoro audio into a transport with no client — the
  audio is silently lost, but the event log still records `tester_transcript` (a transcript for
  speech that never physically happened, which poisons `metrics.json`).
- Before any client has ever connected, `speak()` blocks on `_connected.wait()` with no child-side
  timeout, so the caller gets a generic parent deadline error instead of "the page never opened
  the audio WebSocket — did the app call getUserMedia?".

#### F4 — Ghost speech: child-side waits outlive parent-side deadlines

`server.py` gives `speak` a parent deadline (60/150 s) but the child has no corresponding
timeout on `_wait_for_app_bot_silent()` or `_connected.wait()` (`agent.py:612-615,637-640`).
When the deadline expires, the parent raises `TimeoutError` to the MCP client — but the child
task keeps waiting and **will still speak later**, whenever the bot finally goes silent. The
LLM has been told the speak failed; the synthetic user then talks anyway, desynchronizing the
scripted conversation and the event log from what the LLM believes happened. The same pattern
applies to any future long child-side wait: the child must enforce a timeout slightly *below*
the parent's deadline and reply with an `error`, so the two sides always agree on whether an
utterance happened.

**Worked example** (walk through `speak("Sorry, what?", wait_for_turn=True)`):

1. The **parent** (MCP server) puts the command on the queue and starts a stopwatch — it will
   wait at most 150 s for the child's answer (`deadline=150.0` in `server.py`).
2. The **child** picks it up. `wait_for_turn=True` means "be polite: wait until the app's bot
   stops talking, then speak" — so it sits in `_wait_for_app_bot_silent()`, which has **no
   timeout of its own**. It will wait forever if it has to.
3. Suppose the app's bot rambles for 4 minutes (or its VAD misbehaves and never signals
   silence). At the 150 s mark the parent gives up: the `speak` tool call returns
   `TimeoutError` to the LLM. From the LLM's point of view: *"my speak failed; nothing was
   said."*
4. But nobody told the child to stop. Its task is still waiting. At minute 4 the bot finally
   goes quiet — and the child does exactly what it was asked: it speaks "Sorry, what?" into
   the mic.

That's the ghost: audio coming out of the synthetic user that the driving LLM believes was
never delivered. It's especially bad for a *testing* tool because everything downstream trusts
the record: the LLM, told the speak failed, has likely retried or moved on — so the tester says
things twice or out of order; the event log records `tester_transcript`/`tester_speech_started`
for the ghost utterance, so `metrics.json` is computed from a conversation that doesn't match
the scenario the LLM thought it ran; and the app bot under test *hears* the ghost and responds
to it, drifting the rest of the session off-script. The `_connected.wait()` before speaking
(waiting for the browser page to connect) is the same bug through a different door. The fix is
one rule — **the child must always give up slightly before the parent does** — implemented by
T5.

#### F5 — CI never runs the tests or the type checker, and targets the wrong Python

- `.github/workflows/build.yaml` and `publish.yaml` run `uv python install 3.10`, but
  `pyproject.toml` declares `requires-python = ">=3.11"` and the code uses 3.11+ syntax.
- No workflow runs `pytest` — `tests/test_metrics.py` exists but is dead weight in CI.
- No workflow runs `pyright`, though it's a documented pre-commit check and a dev dependency.

#### F6 — README documents the pre-Stage-2 tool surface

`README.md` still says `listen(timeout=30)` "returns the transcribed text, or `""` on timeout"
and shows `speak(text)` with no other parameters, no `record_dir`, no events, no barge-in, no
artifacts. Anyone onboarding from the README learns an API that no longer exists; the accurate
descriptions live only in `server.py` docstrings and `CLAUDE.md`.

#### F7 — `agent_ipc` module-global singleton limits testability and hides edge cases

The parent↔child mailbox is module-level mutable state (`_cmd_queue`, `_response_queue`,
`_pending`, `_router_task`) shared by both the parent *and* re-assigned inside the child entry
point. It works, but:

- Nothing in the IPC layer is unit-testable without spawning a real child; the router's
  session-replacement logic (`_response_router`'s "queue was swapped" branch) is subtle and has
  zero tests.
- `_fail_pending` clears the *global* `_pending`; a command registered for a new session while
  the old session's router is still winding down can be failed by the old router.
- "One session per process" is baked in at module level rather than being a policy of the server.

#### F8 — The shim's pre-connection buffer is unbounded and never releases AudioData

`shim.js:77-92,129-134`: inbound frames arriving before the page calls `getUserMedia` accumulate
in `pendingInbound` forever. `AudioData` objects hold audio memory that must be `close()`d; if
the page never asks for a mic (wrong page, app bug), memory grows for the life of the tab, and
if the page asks late, the entire backlog bursts into the new mic track as a garbled
faster-than-real-time stream. A bounded, drop-oldest (and `close()`-on-drop) buffer preserves
the useful "don't lose the first word" behavior without the pathology.

#### F9 — End-to-end verification requires a private app; contributors can't run it

`scripts/e2e_readme_call.py` was coupled to one specific private app (Ember: its login form, its
URL structure, its button labels) — it has since been deleted outright rather than kept and
fixed (see T1). The only app-independent checks are the smoke scripts, which exercise the audio
plumbing but not a real `getUserMedia` + `RTCPeerConnection` round trip. There is no fixture a
contributor (or CI) can run the full loop against. T9 (fixture app) is now the only planned path
to full-loop verification that doesn't require a private app.

#### F10 — Tuning knobs are hardcoded

`VAD_STOP_SECS` (agent.py:89), Kokoro `voice_id="af_heart"` (agent.py:686), the Whisper model
names (agent.py:676-683), TTS speed (kokoro_tts.py:168), and the MCP host/port (server.py:33-39,
fixed at import time) are all constants. Different target apps legitimately need different VAD
windows and voices; testing accents/speeds is a core synthetic-user feature that currently
requires editing source.

#### F11 — Armed barge-in triggers can't be inspected or disarmed

`speak(when=...)` arms a one-shot background task; the only way to cancel it is `stop()`. A
scenario that arms a trigger and then changes plan (bot said something unexpected) will have the
trigger fire mid-conversation. There is also no way to ask "what's currently armed?" other than
replaying the event log and pairing `armed`/`fired` events by hand.

#### F12 — Known capability gap: cross-origin iframes and Web Workers (accepted, tracked)

The `RTCPeerConnection` wrap can't see peer connections inside cross-origin iframes (Daily
Prebuilt) or workers. The README already names the workaround — tap `<audio>` elements via
`MutationObserver` + `captureStream()`. This is the single biggest *reach* improvement (it would
also cover apps that play TTS through `<audio>` without WebRTC), but it's experimental and must
not destabilize the existing tap.

#### F13 — Minor hygiene (bundle into other tasks)

- `pipecat-ai>=1.3.0` has no upper bound; history shows pipecat minor upgrades required code
  changes (`87d9bd5`). Pin `<2` like the other deps.
- `asyncio.get_event_loop()` in `agent_ipc.py`/`agent.py` — deprecated inside running loops;
  use `get_running_loop()`.
- `_PipelineEventObserver._seen_frame_ids` and the agent's `_events` grow unboundedly — fine
  for test-length sessions, worth a comment stating that assumption.
- `logger.debug(f"...")` with no placeholders in several spots (ruff would catch with `G`/`F`
  rules if enabled later; not worth a dedicated task).

---

## Part 2 — Improvement plan (delegation-ready tasks)

### How to read a task

Each task is written as a contract for a subagent. **Objective** is the outcome; **Contract**
is the behavior/API the change must implement and the invariants it must not break;
**Verify** is what "done" means. Tasks say *what*, not *how* — no code is prescribed.

### Conventions binding every task (paste into each subagent prompt)

- Work from `CLAUDE.md` first; it documents the verified traps. If your change invalidates a
  documented fact, update `CLAUDE.md` (and the relevant docstrings) in the same PR.
- Python ≥ 3.11, `uv` for everything. BSD-2-Clause license header on every new `.py`.
  Google-style docstrings (ruff `D` is enforced), line length 100.
- Before committing: `uv run ruff check src/ && uv run ruff format src/ && uv run pyright src/`
  all clean, and `uv run pytest` green.
- Run artifacts go under `temp/` only. Never commit anything under `temp/`.
- Do not change the MCP tool surface (names/required params/return shapes) unless the task
  explicitly says so; the tools are the public API.
- One task = one PR-sized branch. No drive-by refactors outside the task's file list.

### Executor guidance

- **Sonnet 5** — well-bounded tasks whose contract fully specifies the behavior: docs, CI,
  config plumbing, mechanical hardening with clear acceptance tests.
- **Opus 4.8** — tasks requiring design judgment across process/async boundaries, tricky
  concurrency, or building new test infrastructure.

Dependency graph (tasks not listed as depending on anything can start immediately, in
parallel):

```
W1: T1  T2  T3                 (independent, start now)
W2: T4  T5(→T4 helpful)  T6    (runtime robustness)
W3: T7 ──► T8 ──► T9           (testability ladder)
W4: T10  T11  T12  T13         (capabilities; T12 after T9 if possible)
```

---

### Workstream 1 — Hygiene & trust (do first)

#### T1 — Purge committed credentials [DONE — driver deleted outright]

- **Executor:** Sonnet 5 · **Size:** S · **Finding:** F1 · **Depends on:** nothing
- **Status:** Done. `scripts/e2e_readme_call.py` and `notes.md` were deleted outright (git rm),
  not made configuration-driven — the maintainer decided the app coupling made it not worth
  keeping around; full-loop verification is now tracked by T9 (fixture app) instead. All
  references to the deleted script were scrubbed from `CLAUDE.md`, `README.md`, and this plan.
- **Remaining scope (still open, human-only):** the leaked email + password are in git history on
  a public repo and **must be rotated by a human** — an agent cannot do this and must not try.
  History rewriting remains out of scope; treat the credential as permanently burned regardless.
  `.env` hygiene (gitignored secrets, no defaults that are real credentials) applies to any future
  driver that talks to a private app.

#### T2 — CI actually gates: tests, types, correct Python, pinned pipecat

- **Executor:** Sonnet 5 · **Size:** S · **Findings:** F5, F13 · **Depends on:** nothing
- **Contract:**
  - All workflows install a Python matching `requires-python` (3.11; a 3.11+3.12 matrix on the
    test job is welcome but optional).
  - The build/PR workflow runs `uv run pytest` and `uv run pyright src/` as required steps, in
    addition to the existing ruff steps. Tests must not require a browser, models, or network
    (that's what `tests/` means in this repo — see T8/T9 for the heavier tiers).
  - `pyproject.toml`: bound pipecat as `>=1.3.0,<2`.
  - `uv.lock` refreshed if the bound changes resolution; no other dependency changes.
- **Verify:** CI green on the PR itself; deliberately breaking a metrics test locally makes
  `uv run pytest` fail (proving the gate is live).

#### T3 — README refresh to the shipped tool surface

- **Executor:** Sonnet 5 · **Size:** S · **Finding:** F6 · **Depends on:** nothing
- **Contract:**
  - The MCP-tools table and the example session in `README.md` describe the *current* four
    tools exactly as `server.py` docstrings define them: `listen` returns
    `{events, cursor}` with the event vocabulary (summarized, linking to `events.py` for the
    full list); `speak`'s `wait_for_playout` / `wait_for_turn` / `when`+`timer_secs` semantics;
    `start_browser_session`'s `record_dir` and the artifact set; `stop()`'s `artifacts` return.
  - Add a short "test report" subsection showing what `metrics.json` contains (schema summary,
    not the full spec — link the Stage 4 design doc).
  - Keep the topology diagram, sharp-edges list and CDP-attach instructions; correct anything
    that contradicts `CLAUDE.md` (which is the source of truth where they disagree).
  - Fix the duplicated `# voicebox` heading.
- **Out of scope:** any code change.
- **Verify:** every tool signature and return key mentioned in the README exists verbatim in
  `server.py`; a cold reader can run the example session against the docstrings without hitting
  a renamed parameter.

---

### Workstream 2 — Runtime robustness

> **Piggyback obligation (from T2's landing) — DONE:** CI's pyright step was
> `continue-on-error` because of 4 pre-existing type errors: `agent.py:389`
> (`start_recording` on an `Optional`), and `browser_session.py:30,31,73`
> (`multiprocessing.Event` used as a type annotation; `.wait()` on an `Optional`). T4 fixed the
> `browser_session.py` ones; T5 fixed the `agent.py` one (local-binding narrowing) and, as the
> second lander, flipped the CI pyright step back to required. `uv run pyright src/` is fully clean.

#### T4 — Pipecat-child readiness handshake and startup failure surfacing

- **Executor:** Opus 4.8 · **Size:** M · **Finding:** F2 · **Depends on:** nothing
- **Objective:** `start_browser_session` returns only when the audio child is actually ready to
  serve commands, and a child that fails or is slow to start produces an attributable,
  actionable error at the tool boundary.
- **Contract:**
  - The child reports readiness to the parent after its transport is bound and STT/TTS services
    are constructed (i.e. after model download/load). The mechanism is the implementer's choice
    but must ride the existing IPC (queues/process machinery in `agent_ipc.py`), not a new side
    channel.
  - `start_browser_session` waits for both children with a generous default timeout and
    distinguishes three failures in its error message: audio child startup error (include the
    child's exception text), audio child timeout (name the likely cause: first-run model
    download; state the cache dir `~/.cache/kokoro-onnx` and that Whisper downloads via its own
    cache), and browser startup failure (existing behavior).
  - First-run model downloads must produce at least one parent-visible log line before and
    after, so a long wait is explainable from the server's stderr.
  - A child that dies *after* startup keeps being handled as today (router fails pending
    futures) — do not regress that path.
  - Timing note: the browser child and audio child may start concurrently (they are independent);
    preserve the existing cleanup-on-failure behavior (`stop_pipecat_process()` if the browser
    fails, and now vice versa).
- **Out of scope:** pre-fetching models at install time (may be noted as a future `voicebox
  --prefetch` idea in the design doc, not built).
- **Verify:** (a) with a warm model cache, `start_browser_session` succeeds and the first
  `speak` works with no sleep in between (the smoke scripts' `await asyncio.sleep(2)` after
  `start_pipecat_process` becomes deletable — delete it); (b) simulate a startup crash (e.g.
  point Kokoro at an invalid model path via a temporary env/arg) and confirm the tool call
  fails with the child's error text, ports released, no zombie processes (`ps` clean).

#### T5 — Coherent connection-state and deadline semantics for `speak` [DONE]

- **Executor:** Opus 4.8 · **Size:** M · **Findings:** F3, F4 · **Depends on:** T4 (readiness
  makes "not connected" unambiguous), can be built standalone if T4 is delayed
- **Status:** Done. Shared constants live in `src/voicebox/timeouts.py` (imported by both
  `server.py` and `agent.py`, no circularity); the parent deadline is `speak_parent_deadline()`
  = child budget + `IPC_MARGIN_SECS` (15 s), grace period `CONNECT_GRACE_SECS` = 10 s. Connection
  tracking is stateful (`_connected` cleared on disconnect). New event
  `tester_barge_in_dropped` (with `reason`) is logged when an armed trigger fires while
  disconnected. Piggyback done: `agent.py` `start_recording` narrowing fixed and the CI pyright
  step flipped back to required (`.github/workflows/build.yaml`). Unit-verified in
  `tests/test_speak_deadlines.py`; the live audio path is covered by `scripts/smoke_reconnect.py`,
  which the maintainer must run on localhost (models can't be fetched in the sandbox).
- **Objective:** `speak` never silently talks into a dead transport, never speaks *after* the
  caller was told it failed, and disconnect/reconnect round-trips work.
- **Contract:**
  - Connection tracking becomes stateful: the agent knows whether a shim client is currently
    connected; disconnect clears the state, reconnect restores it (the existing
    `client_connected`/`client_disconnected` events keep firing exactly as today).
  - `speak` on a session with no connected client waits up to a short, child-enforced grace
    period for a (re)connection, then fails with an error naming the actual situation ("no
    browser client connected to the audio WebSocket — has the page called getUserMedia?").
    It must NOT emit `tester_transcript` unless the speech is actually being queued to a live
    transport — the event log's ground-truth property is the invariant here.
  - Every child-side wait inside `speak` (`wait_for_turn`'s silence wait, connection waits, the
    armed-trigger path's post-fire connection wait) gets a timeout strictly below the parent
    deadline for that command shape, and expiry resolves the command with an `error` response.
    Invariant: **after the parent has surfaced an error for a speak, that speak can never
    produce audio.** Armed triggers (`when=`) are exempt from parent deadlines by design (they
    return `{armed: True}` immediately) but must respect the connection rules when they fire —
    a trigger that fires while disconnected logs a distinguishable event instead of speaking
    into the void (extend `events.py` with a suitable event type; follow the existing naming
    scheme and docstring style).
  - The parent-side deadline constants in `server.py` and the child-side timeouts must be
    derived from each other or from shared constants — no two magic numbers that can drift.
  - Update `CLAUDE.md`'s tool documentation and `server.py` docstrings for any
    caller-observable change.
- **Verify:** extend `scripts/smoke_full_duplex.py` (or add a sibling smoke script) to cover:
  speak → reload page → speak again (second speak either succeeds after reconnect or fails with
  the named error; no silent success); and a `wait_for_turn` speak whose deadline expires must
  produce no `tester_speech_started` event afterward (poll the event log to prove it).

#### T6 — Shim inbound-buffer bounds and AudioData lifecycle [DONE]

- **Executor:** Sonnet 5 · **Size:** S · **Finding:** F8 · **Depends on:** nothing
- **Status:** Done. `pendingInbound` (`shim.js`) is now bounded by total buffered duration —
  `PENDING_INBOUND_MAX_SECS = 3` (≈576 KB of Float32 PCM worst case), tracked via each
  `AudioData.numberOfFrames` since inbound WS chunks are variable-length (frame count alone isn't a
  stable proxy for duration). Overflow drops the oldest frames and `close()`s them. New diagnostics:
  `pendingInboundMaxSamples`, `droppedInboundFrames`, `droppedInboundSamples`,
  `lastMicHandoffFrames`, `lastMicHandoffSamples`. Fan-out (`writeToMicWriters`), the track-id
  dedupe, and Hook 2 are untouched. Verified live (no models needed) by
  `scripts/smoke_shim_buffer_bound.py`: a fake WS server bursts 6s of audio at a page before
  `getUserMedia`; observed `droppedInboundFrames=30`, `droppedInboundSamples=144000` (exactly the
  3s bound), and the eventual mic track's handoff (`lastMicHandoffSamples=144000`) equals the bound
  and is well under the 288000 samples sent — proving drop-oldest + near-live handoff, not a full
  backlog replay.
- **Contract:**
  - `pendingInbound` becomes a bounded buffer (on the order of a few seconds of audio at
    MIC_RATE; pick a constant and document why). On overflow, the oldest frames are dropped
    and every dropped `AudioData` is `close()`d.
  - Add drop counters to `window.__voiceShim` diagnostics (e.g. dropped-frame count and total
    dropped bytes), following the existing diag naming style.
  - Frames handed to a writer keep today's semantics (write consumes; clones for extra
    writers). No change to the fan-out logic or the dedupe logic.
  - Keep the shim's prime directives: never throw during install, every hook gated on API
    presence, `__voiceShim` always present.
- **Verify:** `scripts/smoke_browser_shim.py` still passes; add a check to it (or
  `debug_shim_load.py`) that speaks for longer than the buffer bound *before* any
  `getUserMedia` call and asserts the drop counter rose while memory stayed bounded and the
  eventual mic track starts near-live rather than replaying the whole backlog.

---

### Workstream 3 — Testability ladder (sequential)

#### T7 — Encapsulate the IPC session (kill the module-global mailbox)

- **Executor:** Opus 4.8 · **Size:** L · **Finding:** F7 · **Depends on:** nothing (but land
  before T8)
- **Objective:** The parent↔child channel becomes an object with an explicit lifecycle, so it
  can be constructed with injected queues in tests and so session-replacement edge cases are
  structurally impossible instead of carefully avoided.
- **Contract:**
  - One class (parent side) owns: the two queues, the child `Process`, the pending-futures map,
    and the router task. Constructing a new session cannot interact with a previous session's
    pending futures (per-instance state, not module globals). Public operations: start, stop,
    send_command (same signature/semantics as today including `deadline`), plus whatever T4
    added for readiness.
  - The child-side helpers (`read_request`/`send_response`) take their queues explicitly
    (passed through the process entry point) instead of reading module globals.
  - `server.py` keeps its current behavior: single active session, `start_browser_session`
    replaces any existing one, `stop()`/`main()` clean up. The single-session *policy* now
    lives in `server.py`, not in `agent_ipc.py`'s shape.
  - External behavior is unchanged: same commands, same error semantics, same `spawn` start
    method (that constraint is documented and stays), same cleanup ordering
    (terminate→kill escalation, queue close/join).
  - The smoke scripts import from `agent_ipc` today — update them to the new construction; keep
    the diff mechanical.
  - Update `CLAUDE.md`'s `agent_ipc.py` row.
- **Verify:** `uv run python scripts/smoke_full_duplex.py` passes (it exercises correlation
  IDs, out-of-order responses, and concurrent commands — the exact machinery being moved);
  plus the new unit tests from T8 if landed together, otherwise a minimal test proving two
  sequential sessions in one process don't cross-talk.

#### T8 — Unit-test tier for the protocol layer (runs in CI, no browser, no models)

- **Executor:** Opus 4.8 to establish the harness; follow-up coverage suitable for Sonnet 5 ·
  **Size:** M · **Findings:** F5, F7 · **Depends on:** T7, T2
- **Objective:** The concurrency-bearing code — the response router, deadline behavior, the
  bot command loop, event-log semantics — gets fast deterministic tests, so IPC regressions
  are caught before a human runs a browser.
- **Contract:**
  - Tests live under `tests/`, run with plain `uv run pytest` in seconds, import no pipecat
    pipeline, launch no browser, download nothing. Where the real child process is too heavy,
    test against the same queue interface with an in-test fake child (a thread or task that
    speaks the request/response dict protocol).
  - Minimum behaviors covered: correlation-id routing under out-of-order responses; a
    deadline-expired command's late response is dropped harmlessly; child-death fails pending
    futures with the documented error; session replacement cannot fail the new session's
    futures (the T7 guarantee); the bot loop dispatches commands concurrently, converts
    exceptions to `error` responses, and its `stop` path answers in-flight listens before
    exiting; `listen_events` cursor semantics (clamping, resume, timeout-empty) — this may
    require testing the agent's event log in isolation from the pipeline (constructing the
    agent without starting it is acceptable if that's the clean seam; choose the seam, don't
    force it).
  - Keep the existing `tests/test_metrics.py` style: plain pytest, synthetic event dicts,
    documented scenario comments.
- **Verify:** CI (from T2) runs the suite; total runtime of `tests/` under ~30 s; coverage of
  the listed behaviors demonstrable by pointing at specific tests in the PR description.

#### T9 — Self-contained fixture voice app + reproducible integration test

- **Executor:** Opus 4.8 · **Size:** L · **Findings:** F9, F1 · **Depends on:** T8 (nice to
  have), T4 (uses readiness instead of sleeps)
- **Objective:** Anyone (including CI on a Linux runner with the pre-installed Chromium) can
  run the *full* loop — synthetic mic → page → WebRTC → remote track → tap → STT — against a
  fixture that ships with the repo, with zero private apps or accounts.
- **Contract:**
  - A minimal static page (served locally by the test itself) that behaves like a voice app:
    calls `getUserMedia({audio})`, builds a local `RTCPeerConnection` loopback (two peers in
    the same page), and plays the received track — so the shim's *both* hooks are exercised by
    a genuine WebRTC path, not stubs. Optionally the page delays/echoes the audio so
    `listen()` has something to transcribe from our own Kokoro speech ("echo test": speak a
    phrase, assert the transcript resembles it).
  - A driver (pytest marker like `-m integration`, or a script under `scripts/` consistent
    with the existing ones — prefer pytest so CI can select tiers) that: starts the session
    against the fixture page, speaks, asserts `tester_speech_*` events and shim counters
    (`inboundChunks`, `outboundChunks`, `perTrackBytes`) advance, asserts an
    `app_bot_transcript` arrives on the echo path, runs `stop()` with `record_dir` under
    `temp/` and asserts the artifact set exists with plausible durations.
  - Must run headless on Linux. If Whisper-model download makes it too heavy for the default
    CI job, gate it behind a manually-triggered/nightly workflow — but the test itself must
    not know or care whether it's in CI.
  - Document in `CLAUDE.md` (dev-workflow section) as the third verification tier:
    unit (`pytest`), integration (fixture app), e2e (`scripts/` against a real app).
- **Out of scope:** testing cross-origin iframes (that's T12's concern). (The former
  `e2e_readme_call.py` real-app driver has been deleted outright per T1 — this fixture is now the
  only planned full-loop verification.)
- **Verify:** fresh clone → `uv sync` → run the integration tier → green, on a machine that
  has never seen the readme app.

---

### Workstream 4 — Capability & ergonomics

#### T10 — Configuration surface for the tuning knobs

- **Executor:** Sonnet 5 · **Size:** M · **Finding:** F10 · **Depends on:** nothing; touches
  the same lines as T4/T5 in `agent.py`, so coordinate ordering
- **Objective:** The per-session knobs a test author actually varies are settable without
  editing source, with today's values as defaults.
- **Contract:**
  - `start_browser_session` gains optional parameters for: VAD stop seconds, TTS voice id, and
    TTS speed. They flow through `BrowserShimRunnerArguments` to the child (that dataclass is
    the established vehicle — extend it, keeping its docstring discipline). Invalid values fail
    the tool call with a message naming the parameter.
  - `session_started`'s event payload continues to carry the *effective* `vad_stop_secs` (it
    already does; verify it reflects the override, since `metrics.json` biases depend on it —
    check `compute_metrics`' `vad_stop_secs` plumbing too).
  - The `voicebox` CLI gains flags for MCP host/port and log level (the FastMCP instance is
    currently constructed at import time with a fixed port — restructure only as much as
    needed for the flags to take effect).
  - Whisper model choice may be exposed as an env var rather than a tool param (model swaps
    are a deployment decision, not a per-session one) — document whichever is chosen.
  - Update `server.py` docstrings, `CLAUDE.md` and README (post-T3) for the new knobs.
- **Verify:** smoke script run with a non-default voice and `vad_stop_secs` shows both taking
  effect (different voice audible in the recorded WAV; `session_started.vad_stop_secs` equals
  the override); `voicebox --port 9095` serves on 9095.

#### T11 — Barge-in trigger management: inspect and disarm

- **Executor:** Sonnet 5 · **Size:** M · **Finding:** F11 · **Depends on:** T5 (shares the
  armed-task code paths)
- **Objective:** Armed one-shot triggers become first-class: the LLM can see what's armed and
  cancel it when the scenario changes.
- **Contract:**
  - Arming returns an identifier. A new small MCP tool (or a `speak` companion — prefer a
    dedicated tool for discoverability; naming consistent with the existing four) lists armed
    triggers (`when`, `timer_secs`, text, armed-at) and disarms by id or all.
  - Disarming emits a new event type (extend `events.py`; follow the `tester_barge_in_*`
    naming family) so the log stays a complete record.
  - A trigger that fires between the LLM's decision and the disarm call is inherently racy —
    the disarm response must state whether it disarmed or the trigger had already fired, so
    the LLM can react.
  - `stop()` continues to cancel everything armed (existing behavior).
  - Update `CLAUDE.md`'s tool list and `server.py` docstrings.
- **Verify:** extend `scripts/smoke_barge_in.py`: arm, disarm before the trigger event, cause
  the event, assert no speech occurred and the log shows armed→disarmed; arm again without
  disarming and assert the original fire path still works.

#### T12 — `<audio>`-element tap fallback (cross-origin iframes / non-RTC playout)

- **Executor:** Opus 4.8 · **Size:** L, experimental · **Finding:** F12 · **Depends on:**
  best done after T9 so the fixture can grow an `<audio>`-only variant to test against
- **Objective:** Apps whose bot audio never surfaces as a same-origin `RTCPeerConnection`
  track (Daily Prebuilt iframe, apps playing TTS via `<audio>`/`Audio()`) become tappable.
- **Contract:**
  - A third hook in `shim.js`: observe media elements (both those present in the DOM and ones
    created but never attached), and for elements playing audio, tee their output into the
    same 16 kHz worklet path used by Hook 2. The README's sketch (MutationObserver +
    `captureStream()`) is the starting hypothesis, not a requirement — whatever mechanism is
    chosen must preserve real-time pacing (the Web-Audio-not-WebCodecs lesson in `CLAUDE.md`
    applies) and must not double-tap audio that Hook 2 already captures (dedupe across hooks;
    track-id dedupe exists — extend the concept, and prove it with the fixture: a page that
    both plays a remote track *and* mirrors it through an `<audio>` element must not produce
    doubled audio in the tap).
  - Same defensive rules: gated on API presence, never throws, diagnostics on `__voiceShim`
    (per-hook counters, so a session can tell *which* hook is feeding STT).
  - Feature-flagged off by default at first (a `start_browser_session` param or shim
    constant), documented as experimental in `CLAUDE.md`'s known-limitations entry — which
    this task rewrites to describe the new state of the world.
  - Note the boundary honestly in docs: a cross-origin *iframe's internal* elements are
    unreachable from the top frame; what this hook can catch is audio the embedding page
    plays, plus same-origin iframe content. If Daily Prebuilt remains out of reach, the
    limitation entry says so explicitly with what was tried.
- **Verify:** fixture-app variant that plays bot audio only through an `<audio>` element:
  `listen()` produces transcripts with the flag on and records silence with it off; the
  double-tap dedupe case above; `smoke_browser_shim.py` unaffected with the flag off.

#### T13 — Scenario layer (Stage 5 of the original roadmap, still unbuilt)

- **Executor:** Sonnet 5 · **Size:** M (docs + one worked example, minimal code) ·
  **Depends on:** T3 (README accuracy), ideally T9 (a fixture to demo against)
- **Objective:** Turn "how to use voicebox as a test harness" into a repeatable convention:
  persona + goal + behaviors + success criteria, executed by an LLM, judged from
  `events.json`/`metrics.json`.
- **Contract:**
  - A `docs/scenarios.md` (or `SCENARIOS.md` — match repo style) defining the scenario
    format: persona, objective, scripted behaviors (including barge-in via `when=`),
    success criteria expressed as assertions over the event log and metrics (e.g. "mean
    response latency < 2.5 s", "bot stops talking within X ms of barge-in, after subtracting
    `vad_stop_secs`"), and the judge step (LLM reads `turns` + summary and issues pass/fail
    with citations).
  - One fully-worked scenario checked in, runnable against the fixture app from T9 (the readme
    app is no longer an option — its driver was deleted per T1).
  - If the repo adopts a Claude skill for this, it lives where `CLAUDE.md` says skills live;
    otherwise the doc is the deliverable. No new server code beyond, at most, trivial
    metric additions that T13 justifies in writing.
- **Verify:** an LLM agent given only the scenario doc + a running voicebox completes the
  worked scenario and produces a pass/fail judgment citing specific metrics values.

---

## Suggested sequencing

| Sprint | Land |
|---|---|
| 1 | T1, T2, T3 (all parallel, small) |
| 2 | T4, T6 in parallel; then T5 |
| 3 | T7 → T8; T10 in parallel |
| 4 | T9; T11 in parallel |
| 5 | T12 (experimental), T13 |

The review's single most important observation, restated: the June rebuild made the *protocol*
trustworthy; the current gaps are about *trust at the edges* — startup (T4), disconnects and
deadlines (T5), secrets (T1), and the fact that only one machine on earth can currently verify
the full loop (T9). Those four tasks carry most of the value of this plan.
