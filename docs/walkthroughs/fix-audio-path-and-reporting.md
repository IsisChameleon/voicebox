# Walkthrough — `fix/audio-path-and-reporting`

*Status: **in progress**. Started 2026-07-29, walkthrough opened 2026-08-01 (see `BUILDLOG.md` D1).
Branched from `eb89647`.*

Fixes the audio-path and reporting defects found in a dogfood session. Root causes:
[`docs/specs/2026-07-29-field-report-triage.md`](../specs/2026-07-29-field-report-triage.md).
Design: [`docs/specs/2026-07-29-audio-path-and-reporting-fixes.md`](../specs/2026-07-29-audio-path-and-reporting-fixes.md).
Task breakdown and success criteria:
[`docs/specs/2026-07-31-fix-plan-execution.md`](../specs/2026-07-31-fix-plan-execution.md).

## Status

| Task | What it fixes | Commits | Evidence | Done |
|---|---|---|---|---|
| **A** | Session startup fails loudly; `attach_hint` stops destroying the shim tab | `1df7e51` | [t-a-startup-fails-loudly.md](../artefacts/fix-audio-path-and-reporting/t-a-startup-fails-loudly.md) | ✅ |
| **B** | `metrics.json` reconciles with itself (turns from intervals, split gap attribution) | — | — | ⬜ |
| **C** | Phase-0 timing instrumentation; attributes the unexplained ~24 s per-turn lag | `d1ec4e9` | [t-c-timing-instrumentation.md](../artefacts/fix-audio-path-and-reporting/t-c-timing-instrumentation.md) | ✅ |
| **D** | VAD moves upstream of the STT (90 % of speech was trimmed before Whisper) | — | — | ⬜ |
| **E** | Transcription leaves the frame path (non-blocking Whisper worker) | — | — | ⬜ |
| **F** | Transcript-loss holes closed (drain STT before writing artefacts) | — | — | ⬜ |
| **G** | Kokoro plays one utterance as one turn (no mid-utterance silence) | — | — | ⬜ |
| **H** | `listen()` batches time-ordered; docstrings state what timestamps mean | — | — | ⬜ |

Live-only (🔴) acceptance stories across all tasks need a running voice app on
`localhost:3000` and are collected in the execution spec; they are the checklist for the next
dogfood session, not part of any task's ✅.

## Try it

```bash
uv run pytest -q                     # whole suite
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
uv run pyright src/
```

---

## Task A — startup fails loudly; attach no longer destroys the session

*Commit `1df7e51`. Criteria: execution spec § "Task A".*

- `page.goto` failure now propagates to the parent instead of being caught, logged and followed
  by `ready_event.set()` — `start_browser_session` raises with the child's error text rather than
  returning a `cdp_endpoint` pointing at `about:blank`
  (`src/voicebox/browser_session.py`).
- After a successful `goto` the child polls until the URL is non-`about:blank` **and**
  `window.__voiceShim.installed === true`, bounded at 10 s; expiry raises.
- `attach_hint` is now `playwright-cli attach --cdp <endpoint>`. The old `close-all` +
  two-env-var recipe is gone from the hint, from `start_browser_session`'s docstring and from
  `CLAUDE.md`. `playwright-cli open` navigates the current page to `about:blank`, which over CDP
  is voicebox's shim tab — the hint was telling callers to destroy their own audio path.
- The "**Do not open new tabs**" warning survives in `CLAUDE.md`, unchanged in meaning.

**Not covered:** stories A3 and A4 are 🔴 live-only — that the shim is already installed when the
tool returns, and that a real `playwright-cli attach` + `snapshot` leaves the session connected.
Both are asserted by construction (the poll, the hint text) but unproven against a real app.

---

## Task C — Phase 0 timing instrumentation

*Commit `d1ec4e9`. Criteria: execution spec § "Task C". No behaviour change by design.*

- `src/voicebox/timing.py` adds `log_duration()` plus two mixins — `TimedSTTMixin` (times
  `run_stt`) and `TimedTurnAnalyzerMixin` (times `analyze_end_of_turn`). They list **first** in the
  bases so they precede the concrete service in the MRO, and delegate via `super()`, so the same
  mixin composes with a subclass of a concrete service without changing.
- `agent.py` builds the pipeline from `_TimedWhisperSTTService`, `_TimedWhisperSTTServiceMLX` and
  `_TimedSmartTurnAnalyzer` (`agent.py:103-116`), and wraps `on_user_turn_stopped` in
  `log_duration` (`agent.py:376`). Nothing is vendored from pipecat.
- Every line is `voicebox.timing name=<call> secs=<float>` at DEBUG — one grep splits a session
  log by call.

**Why it exists:** a dogfood session showed a steady ~24 s per-turn transcript lag, larger than
warm Whisper throughput (0.40× realtime) accounts for. `LocalSmartTurnAnalyzerV3` is the suspect,
but that is a hypothesis — this task lands the measurement so the next live session attributes it
by name instead of guessing.

**Not covered:** C3 🔴, the live session that actually attributes the 24 s, has not run. Until it
does, the Task D follow-up (dropping `TurnAnalyzerUserTurnStopStrategy`) stays unjustified.
`on_user_turn_stopped` is instrumented but untested. Full list in the evidence artefact.

## Task B — *(in flight, not committed)*
